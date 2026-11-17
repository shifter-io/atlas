"""The 24 estimated hours of one daily test, from the network's hour formula (formulas/*.json).

A test of a network and country measured at UTC hour mh gives the hours mh .. mh+23 (into the
next day): each hour's unique IPs are the measured IPs x that hour's factor over the measured
hour's (exactly the hour factor when the test ran at the peak, as scheduled), its requests the
measured requests x the request curve's ratio, and the day's unique IPs the measured IPs x the
daily factor over the measured hour's. A country on the network curve (`curve` "network") takes
the network's request curve too; its hour and daily factors already come from that curve. An
hour whose factor is null (no data in the formula: the network's campaign saw no exit IP then) is
not estimated: its row has null figures, `estimated` false and the formula's reason, and it is
never stored, so no sum or average counts it as 0. A country hour whose factor comes from the
network curve or a borrowed curve (`hour_basis` network_missing, network_low_sample or borrowed)
has too few request rows of its own to scale requests by, so its requests follow its hour factor
instead; the same holds for every hour when the measured hour is such an hour.

The test's hour is the hour it was scheduled for (`scheduled`, the daily campaign's slot), not
the hour its run started: a catch-up, or a country that waited for others on its droplet, still
counts for and is scaled by its own hour (ESTIMATE-SCHED-1, #1152). Without a scheduled hour the
run's start counts. Its hour factor is never taken below BASE_FLOOR, so no test is scaled by
more than 1 / BASE_FLOOR times its daily factor (a night hour of 0.14 made IPRoyal's x19).
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .. import legacy_names, storage, storage_batch

SCHEMA = "atlas-hour-formula"
SCHEMAS = (SCHEMA, legacy_names.HOUR_FORMULA)  # a manager's pre-rename copy reads too
FALLBACK_HOURS = ("network_missing", "network_low_sample", "borrowed")
BASE_FLOOR = 0.5  # the least hour factor a test is scaled by


def load_formula(directory: Path, network: str) -> dict:
    """The network's formula JSON; ValueError for another document or schema version."""
    formula = json.loads((directory / f"{network}.json").read_text())
    if formula.get("schema") not in SCHEMAS or formula.get("schema_version") != 1:
        raise ValueError(f"{network}: not a version 1 hour formula")
    return formula


def _curve(formula: dict, country: str) -> tuple[list, float, list, str, list]:
    entry = formula["countries"].get(country)
    if entry is None:
        raise ValueError(f"{country} is not in the formula")
    requests = formula["network_curve"]["requests"] if entry["curve"] == "network" else None
    # Formula version 1 has no hour_basis: every hour rests on its own count.
    fallback = [basis in FALLBACK_HOURS for basis in entry.get("hour_basis") or [None] * 24]
    return (
        entry["hour_factors"],
        entry["daily_factor"],
        requests or entry["requests"],
        entry["curve"],
        fallback,
    )


def hours(
    formula: dict,
    country: str,
    measured_at: datetime,
    ips: int,
    requests: int,
    scheduled: datetime | None = None,
) -> list:
    """The estimated rows of a test measured at `measured_at` with `ips` and `requests`, from
    the hour it was scheduled for when given."""
    factors, daily, curve_requests, curve, fallback = _curve(formula, country)
    start = (scheduled or measured_at).astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    if not factors[start.hour]:
        raise ValueError(f"{country}: the measured hour {start.hour} has no factor")
    base = max(factors[start.hour], BASE_FLOOR)
    base_requests = 0 if fallback[start.hour] else curve_requests[start.hour]
    rows = []
    for offset in range(24):
        at = start + timedelta(hours=offset)
        if factors[at.hour] is None:
            rows.append(_not_estimated(formula, at, curve))
            continue
        factor = factors[at.hour] / base
        own = base_requests and not fallback[at.hour]
        ratio = curve_requests[at.hour] / base_requests if own else factor
        rows.append(
            {
                "day": at.date(),
                "hour": at.hour,
                "hour_factor": factor,
                "unique_ips": ips * factor,
                "requests": requests * ratio,
                "day_unique_ips": ips * daily / base,
                "curve": curve,
                "estimated": True,
            }
        )
    return rows


def _not_estimated(formula: dict, at: datetime, curve: str) -> dict:
    reasons = formula["network_curve"].get("no_data_reasons") or [None] * 24
    figures = dict.fromkeys(("hour_factor", "unique_ips", "requests", "day_unique_ips"))
    reason = reasons[at.hour] or "no data: the formula has no factor for this hour"
    return {"day": at.date(), "hour": at.hour, **figures, "curve": curve} | {
        "estimated": False,
        "reason": reason,
    }


# Every run's measurement, in the order asked (idx): a run not imported has no row.
MEASURED = """SELECT w.idx,r.run_key,r.started_at,s.product_id,
 (SELECT count(DISTINCT ip_id) FROM ip_observations o WHERE o.run_key=r.run_key) AS ips,
 (SELECT coalesce(sum(count),0)::bigint FROM ip_observations o WHERE o.run_key=r.run_key)
 AS requests
FROM unnest(%s::uuid[]) WITH ORDINALITY AS w(run_id,idx)
JOIN test_runs r USING(run_id) JOIN scope_stats s USING(scope_id)"""

COLUMNS = (
    "product_id,country_code,day,hour,run_key,measured_at,measured_ips,measured_requests,"
    "hour_factor,unique_ips,requests,day_unique_ips,curve,formula_version"
)
INCOMING = (
    "CREATE TEMP TABLE incoming_estimates ON COMMIT DROP AS SELECT 0 AS idx,"
    f"{COLUMNS} FROM estimated_hours WITH NO DATA"
)
# An hour stays with the latest test that covers it (the later run of the batch on a tie), as
# storing the runs one at a time in order leaves it; released hours never change.
UPSERT = f"""INSERT INTO estimated_hours AS e({COLUMNS})
SELECT DISTINCT ON (product_id,country_code,day,hour) {COLUMNS} FROM incoming_estimates
ORDER BY product_id,country_code,day,hour,measured_at DESC,idx DESC
ON CONFLICT (product_id,country_code,day,hour) DO UPDATE SET run_key=EXCLUDED.run_key,
 measured_at=EXCLUDED.measured_at,measured_ips=EXCLUDED.measured_ips,
 measured_requests=EXCLUDED.measured_requests,hour_factor=EXCLUDED.hour_factor,
 unique_ips=EXCLUDED.unique_ips,requests=EXCLUDED.requests,
 day_unique_ips=EXCLUDED.day_unique_ips,curve=EXCLUDED.curve,
 formula_version=EXCLUDED.formula_version,computed_at=now()
WHERE e.released_at IS NULL AND e.measured_at <= EXCLUDED.measured_at
RETURNING e.run_key,e.country_code"""
FIGURES = ("day", "hour", "hour_factor", "unique_ips", "requests", "day_unique_ips", "curve")


def _rows(runs: list, measured: dict) -> list[tuple]:
    """Every run's estimated hours as incoming_estimates rows; a run's own error if one fails."""
    rows = []
    for idx, (run_id, country, formula, *scheduled) in enumerate(runs, 1):
        if idx not in measured:
            raise ValueError(f"run {run_id} is not imported")
        run_key, measured_at, product_id, ips, requests = measured[idx]
        run = (run_key, measured_at, ips, requests)
        at = scheduled[0] if scheduled else None
        for row in hours(formula, country, measured_at, ips, requests, at):
            if row["estimated"]:
                day, hour, *figures = (row[name] for name in FIGURES)
                version = formula["formula_version"]
                rows.append((idx, product_id, country, day, hour, *run, *figures, version))
    return rows


def estimate_runs(credentials, runs: list, *, conn=None) -> list[int]:
    """Store the estimated hours of imported runs, [(run_id, country, formula[, scheduled])]
    (scheduled: the hour the test was due, else its run's start counts), in one transaction;
    returns the rows each run wrote. With conn (an open connection, idle) they go over it and
    credentials is not read.

    Released hours never change, and an hour stays with the latest test that covers it, so a
    rerun (or an earlier test imported late) is a no-op for them. One round trip reads the runs
    and one writes all their hours, whatever their number. An hour two runs of the batch both
    cover is counted for the run that keeps it.
    """
    if not runs:
        return []
    if conn is None:
        with storage.connect(credentials) as own:
            return _estimate(own, runs)
    return _estimate(conn, runs)


def _estimate(conn, runs: list) -> list[int]:
    conn.autocommit = True  # BEGIN below, in the pipeline: psycopg's own costs a round trip
    found = conn.execute(MEASURED, ([run[0] for run in runs],)).fetchall()
    measured = {idx: rest for idx, *rest in found}
    rows = _rows(runs, measured)
    with conn.pipeline():
        conn.execute("BEGIN")
        conn.execute(INCOMING)
        storage_batch.many(conn, "INSERT INTO incoming_estimates VALUES ", "", None, rows)
        written = conn.execute(UPSERT)
        conn.execute("COMMIT")
    keys = Counter(written.fetchall())
    return [keys[measured[idx][0], runs[idx - 1][1]] for idx in range(1, len(runs) + 1)]


def estimate_run(
    credentials, run_id: str, country: str, formula: dict, scheduled: datetime | None = None
) -> int:
    """Store the estimated hours of one imported run; returns the rows written (estimate_runs)."""
    return estimate_runs(credentials, [(run_id, country, formula, scheduled)])[0]
