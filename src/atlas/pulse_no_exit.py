"""PULSE-NOEXIT-OUTAGE-1 (#1204): a pulse that only got the network's own "no exit" answers.

A network answers a status of its own when it has no exit in the requested country (its
grammar's no_inventory_statuses, availability.no_exit: Evomi 409 or 500, IPRoyal 503). A pulse
whose answers are at least SHARE of those measured nothing about the network's health: the
country has no exit left at that moment, which LOWAVAIL-1 (country_status.py) calls "no exits",
never an outage (16 Nov 2026: Evomi Malawi answered 409 and IPRoyal Central African Republic
503 to every pulse request and two per-country outages opened). So:

- detect (step and fast replay) does not judge such a pulse (no 'down', no per-country outage,
  no success_drop, no recovery) and records the country's first one of a streak as a
  pulse_finished with data reason "no_exits"; outages._pulse_history and the replay leave every
  such pulse out of the later pulses' baselines;
- the pulse evaluator (orchestrator/pulse.py) leaves such runs out of a window's requests and
  errors, so they never make a pulse_down.

The safeguards of the classifier stay. Timeouts and refused connections or logins are never "no
exit" answers, so a run mixing them with the network's code under SHARE is judged as before. The
network guard (guard()): when more than half of the countries the network was pulsed in (at
least country_status.GUARD_MIN) answered "no exit", the network is down, not out of exits, and
every pulse is judged as before. A normally big country (verdicts.BIG IPs or more in a daily
test of the last verdicts.RECENT_DAYS) stays visible until its "no exit" answers have lasted HOLD:
a big pool does not empty in an hour, so the first hours are more likely the network's problem
than the country's, the same reasoning as country_status's two days in a row.
"""

from __future__ import annotations

import re
from datetime import timedelta

from .availability import no_exit
from .country_status import GUARD_MIN
from .providers.grammar import grammar_ids
from .verdicts import BIG, RECENT

SHARE = 0.9  # "no exit" answers from this share of a run's completed requests
HOLD = timedelta(hours=2)  # a normally big country's "no exit" streak is hidden from this long
LOOK = HOLD + timedelta(hours=1)  # the streak is read this far back from the pulse's slot
GUARD = timedelta(hours=1)  # the network guard reads the network's pulses of this last hour
RECORD = "no_exits"  # the pulse_finished data reason of the first hidden pulse of a streak
KEYS = ("requests", "errors", "refused")  # the evaluator's counts per network and region


def _codes_sql(product: str) -> str:
    """The "no exit" statuses of the product's network, as a SQL text[] (none: empty)."""
    cases = [
        f"WHEN '{network}' THEN '{{{','.join(codes)}}}'::text[] "
        for network, codes in ((n, no_exit(n)) for n in grammar_ids())
        if codes and re.fullmatch(r"[a-z0-9-]+", network)
    ]
    found = f"(SELECT p.provider FROM products p WHERE p.product_id={product})"
    return f"CASE {found} " + "".join(cases) + "ELSE '{}'::text[] END"


# Whether a run (r: its test_runs row, br: its batch_runs row) answered mostly "no exit".
EMPTY_SQL = (
    "coalesce((SELECT sum(s.value::bigint) FROM jsonb_each_text(r.summary->'status_codes') s "
    f"WHERE s.key=ANY({_codes_sql('br.product_id')}))"
    f">={SHARE}*nullif((r.summary->>'completed')::bigint,0),false)"
)
NO_EXIT_SQL = f"{EMPTY_SQL} AS no_exit"
PULSES = (
    "FROM test_batches b JOIN batch_runs br USING(batch_id) "
    "LEFT JOIN test_runs r ON r.run_key=br.run_key WHERE b.kind='pulse' "
    "AND br.product_id=%(product)s AND br.status='done' AND br.requests>0 "
)
# The network's countries pulsed in the hour before the slot, and those that answered "no exit".
GUARD_SQL = (
    "SELECT count(DISTINCT b.country_code) AS countries,"
    f"count(DISTINCT b.country_code) FILTER (WHERE {EMPTY_SQL}) AS empty "
    + PULSES
    + "AND b.scheduled_for>%(slot)s-%(guard)s AND b.scheduled_for<=%(slot)s"
)
# The most unique IPs of the country's daily tests of the last verdicts.RECENT_DAYS.
PEAK_SQL = (
    "SELECT coalesce(max(unique_ips),0) AS peak FROM country_verdicts "
    "WHERE product_id=%(product)s AND country_code=trim(%(country)s) "
    f"AND decided_at>%(slot)s-interval '{RECENT}' AND decided_at<=%(slot)s"
)
# The network's pulses in the country over LOOK up to the slot, in slot order.
SERIES_SQL = (
    f"SELECT b.batch_id,b.scheduled_for,{EMPTY_SQL} AS empty "
    + PULSES
    + "AND b.country_code=%(country)s AND b.scheduled_for>=%(slot)s-%(look)s "
    "AND b.scheduled_for<=%(slot)s ORDER BY b.scheduled_for,b.batch_id"
)


def guard(countries: int, empty: int) -> bool:
    """The network guard: of at least GUARD_MIN countries, more than half answered "no exit"."""
    return countries >= GUARD_MIN and empty * 2 > countries


def _streak(rows: list[dict], batch_id: int) -> tuple[list, int]:
    """The series' "no exit" streak that ends with the batch's pulse (slots, oldest first) and
    the index of the batch's pulse in it."""
    end = next((i for i, row in enumerate(rows) if row["batch_id"] == batch_id), None)
    if end is None:
        return [], 0
    start = end
    while start > 0 and rows[start - 1]["empty"]:
        start -= 1
    return [row["scheduled_for"] for row in rows[start : end + 1]], end - start


def verdict(cur, batch: dict, run: dict) -> str | None:
    """None when the run is judged as before (not mostly "no exit", the network guard holds, or
    a normally big country's streak is shorter than HOLD); else RECORD for the first hidden
    pulse of the country's streak and "" for the others."""
    if not run.get("no_exit"):
        return None
    product, slot = run["product_id"], batch["scheduled_for"]
    found = cur.execute(GUARD_SQL, {"product": product, "slot": slot, "guard": GUARD}).fetchone()
    if guard(found["countries"], found["empty"]):
        return None
    params = {"product": product, "country": batch["country_code"], "slot": slot}
    hold = HOLD if cur.execute(PEAK_SQL, params).fetchone()["peak"] >= BIG else timedelta(0)
    rows = cur.execute(SERIES_SQL, params | {"look": LOOK}).fetchall()
    slots, at = _streak(rows, batch["batch_id"])
    first = slots[0] if slots else slot
    if slot - first < hold:
        return None
    return RECORD if at == 0 or slots[at - 1] - first < hold else ""


def strip(rows: list[dict]) -> list[dict]:
    """The evaluator's window rows (per network, region and `empty`, with `countries`, `requests`,
    `errors` and `refused`) as one row per network and region, without the "no exit" runs' of
    each network whose guard does not hold."""
    countries: dict[int, list[int]] = {}
    for row in rows:
        seen = countries.setdefault(row["product_id"], [0, 0])
        seen[0] += row["countries"]
        seen[1] += row["countries"] if row["empty"] else 0
    merged: dict[tuple, dict] = {}
    for row in rows:
        if row["empty"] and not guard(*countries[row["product_id"]]):
            continue
        key = (row["product_id"], row["region_id"])
        if key not in merged:
            merged[key] = {"product_id": key[0], "region_id": key[1]} | dict.fromkeys(KEYS, 0)
        for name in KEYS:
            merged[key][name] += int(row[name] or 0)
    return list(merged.values())
