"""LATENCY-DAY-STORE-1: the per-day latency store that lets the 30d latency change compare the last
30 whole UTC days with the 30 before (latency_days.py), 60 days, while the calculator reads only
RAW_DAYS (30) days of raw rows.

A record is one network's (provider and kind's) latency target on one UTC day: the merged weights
of its time slots with min_latency_successes light-load successes (latency.weighed(): each slot
weighs 1, so the weights of several days add up to their period's, whose p50 is the one blend()
of all its slots gives) and each country's light-load successes. Network.window() makes the
records of the whole days its 30d window's raw rows hold (day_records()). complete() (from
network.attach(), which has the connection) adds the stored records of the earlier days, sets the
30d window's latency_day_delta and writes every record whose content changed, so the first run
fills the store with the last 30 days and a late test is written again. A record written by a
later cutoff is never replaced by an earlier one's (a replay). Records are kept KEEP_DAYS whole
days back from the cutoff's day.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta

from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from . import latency_carry
from .latency import percentiles, weighed
from .latency_days import compare, day_pair
from .underload import light_votes, time_slots

DAYS = 30  # the 30d change's periods, in whole days
# The two periods and the day before them (compare(): a network tested before the earlier
# period is not new), also before FREEZE_HOUR, when the pair is still the day before's.
KEEP_DAYS = 2 * DAYS + 2
DAY = timedelta(days=1)
# The stored records of the days before the raw window's first whole day.
OLDER = (
    "SELECT provider,kind,day,slots,ms,weights,countries FROM calculated_latency_days "
    "WHERE target=%s AND minimum=%s AND day>=%s AND day<%s ORDER BY provider,kind,day"
)
DIGESTS = "SELECT provider,kind,day,digest FROM calculated_latency_days WHERE target=%s AND day>=%s"
WRITE = (
    "INSERT INTO calculated_latency_days AS d VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
    "ON CONFLICT(provider,kind,target,day) DO UPDATE SET minimum=excluded.minimum,"
    "slots=excluded.slots,ms=excluded.ms,weights=excluded.weights,"
    "countries=excluded.countries,digest=excluded.digest,as_of=excluded.as_of "
    "WHERE d.as_of<=excluded.as_of"
)


def utc_day(moment):
    """The start of moment's UTC day."""
    moment = moment.astimezone(UTC)
    return datetime(moment.year, moment.month, moment.day, tzinfo=UTC)


def day_records(rows, target, since, end, minimum):
    """{"target", "minimum", "raw", "days"}: raw is the first whole UTC day from since, days the
    record of every whole UTC day in [since, end) with rows of the latency target (rows: the
    network's bucket rows)."""
    raw = utc_day(since) + (DAY if utc_day(since) < since else timedelta())
    by_day = defaultdict(list)
    for r in rows:
        if r["target"] == target:
            by_day[utc_day(r["bucket_start"])].append(r)
    days = [
        _record(day, own, minimum)
        for day, own in sorted(by_day.items())
        if raw <= day and day + DAY <= end
    ]
    return {"target": target, "minimum": minimum, "raw": raw, "days": days}


def _record(day, rows, minimum):
    successes = Counter()
    for r in rows:
        if not r.get("loaded") and r.get("light", True):  # latency_days._light()
            successes[r["country"] or ""] += r["counters"]["successes"]
    weights, used = weighed(h for *_, h in light_votes(time_slots(rows), minimum))
    return {"day": day, "slots": used, "weights": dict(weights), "countries": dict(successes)}


def _period(records, minimum, start, days):
    """latency_days' period of `days` whole days from start, made of the days' records."""
    weights, used, successes = Counter(), 0, Counter()
    for r in records:
        if start <= r["day"] < start + days * DAY:
            weights.update(r["weights"])
            used += r["slots"]
            successes.update(r["countries"])
    return {
        "start": start,
        "end": start + days * DAY,
        "p50": percentiles(weights, used)["p50"],
        "countries": sorted(c for c, n in successes.items() if n >= minimum),
    }


def delta(records, as_of, minimum):
    """The 30d window's latency change of two 30-day periods from the records of the network's
    days, the first of which is when it was first tested (compare()). A record holds a day's
    merged weights, not each country's, so there is no common-country comparison of them."""
    since = min((r["day"] for r in records), default=None)
    return compare([_period(records, minimum, s, DAYS) for s in day_pair(as_of, DAYS)], since)


def complete(conn, networks, as_of):
    """Set each network's 30d latency_day_delta to the 30-day change from its window's
    "_latency_days" (removed) and the stored records when it has a value (else the window keeps
    its 7-day or day one, network.py), write its changed records and forget the ones before
    KEEP_DAYS; then each window left without a value keeps the last one (latency_carry.py)."""
    keep = utc_day(as_of) - KEEP_DAYS * DAY
    found = {}
    for entry in networks:
        own = entry["windows"].get("30d", {}).pop("_latency_days", None)
        if own is not None:
            found[entry["provider"], entry["kind"]] = (entry["windows"]["30d"], own)
    if found:
        first = next(iter(found.values()))[1]
        target, minimum, raw = first["target"], first["minimum"], first["raw"]
        cur = conn.cursor(row_factory=tuple_row)
        older, digests = defaultdict(list), {}
        for provider, kind, day, slots, ms, weights, countries in cur.execute(
            OLDER, (target, minimum, keep, raw)
        ):
            weights = dict(zip(ms, weights, strict=True))
            older[provider, kind].append(
                {"day": day, "slots": slots, "weights": weights, "countries": countries}
            )
        for provider, kind, day, digest in cur.execute(DIGESTS, (target, raw)):
            digests[provider, kind, day] = digest
        for key, (window, own) in found.items():
            month = delta(older[key] + own["days"], as_of, minimum)
            if month["pct"] is not None or "latency_day_delta" not in window:
                window["latency_day_delta"] = month
            _write(conn, key, own, digests, as_of)
    conn.execute("DELETE FROM calculated_latency_days WHERE day<%s", (keep,))
    latency_carry.carry(conn, networks)


def _write(conn, key, own, digests, as_of):
    """Write the network's records whose content differs from the stored one."""
    for r in own["days"]:
        ms = sorted(r["weights"])
        content = [own["minimum"], r["slots"], ms, [r["weights"][m] for m in ms]]
        countries = dict(sorted(r["countries"].items()))
        digest = hashlib.sha256(json.dumps([*content, countries]).encode()).hexdigest()
        if digests.get((*key, r["day"])) != digest:
            row = (*key, own["target"], r["day"], *content, Jsonb(countries), digest, as_of)
            conn.execute(WRITE, row)
