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
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .. import storage

SCHEMA = "proxy-index-hour-formula"
FALLBACK_HOURS = ("network_missing", "network_low_sample", "borrowed")


def load_formula(directory: Path, network: str) -> dict:
    """The network's formula JSON; ValueError for another document or schema version."""
    formula = json.loads((directory / f"{network}.json").read_text())
    if formula.get("schema") != SCHEMA or formula.get("schema_version") != 1:
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


def hours(formula: dict, country: str, measured_at: datetime, ips: int, requests: int) -> list:
    """The estimated rows of a test measured at `measured_at` with `ips` and `requests`."""
    factors, daily, curve_requests, curve, fallback = _curve(formula, country)
    start = measured_at.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    base = factors[start.hour]
    if not base:
        raise ValueError(f"{country}: the measured hour {start.hour} has no factor")
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


MEASURED = """SELECT r.run_key,r.started_at,s.product_id,
 (SELECT count(DISTINCT ip_id) FROM ip_observations o WHERE o.run_key=r.run_key) AS ips,
 (SELECT coalesce(sum(count),0)::bigint FROM ip_observations o WHERE o.run_key=r.run_key)
 AS requests
FROM test_runs r JOIN scope_stats s USING(scope_id) WHERE r.run_id=%s"""

UPSERT = """INSERT INTO estimated_hours AS e(product_id,country_code,day,hour,run_key,measured_at,
 measured_ips,measured_requests,hour_factor,unique_ips,requests,day_unique_ips,curve,
 formula_version)
VALUES (%(product_id)s,%(country)s,%(day)s,%(hour)s,%(run_key)s,%(measured_at)s,%(ips)s,
 %(measured_requests)s,%(hour_factor)s,%(unique_ips)s,%(requests)s,%(day_unique_ips)s,%(curve)s,
 %(formula_version)s)
ON CONFLICT (product_id,country_code,day,hour) DO UPDATE SET run_key=EXCLUDED.run_key,
 measured_at=EXCLUDED.measured_at,measured_ips=EXCLUDED.measured_ips,
 measured_requests=EXCLUDED.measured_requests,hour_factor=EXCLUDED.hour_factor,
 unique_ips=EXCLUDED.unique_ips,requests=EXCLUDED.requests,
 day_unique_ips=EXCLUDED.day_unique_ips,curve=EXCLUDED.curve,
 formula_version=EXCLUDED.formula_version,computed_at=now()
WHERE e.released_at IS NULL AND e.measured_at <= EXCLUDED.measured_at"""


def estimate_run(credentials, run_id: str, country: str, formula: dict) -> int:
    """Store the estimated hours of one imported run; returns the rows written.

    Released hours never change, and an hour stays with the latest test that covers it, so a
    rerun (or an earlier test imported late) is a no-op for them.
    """
    with storage.connect(credentials) as conn:
        run = conn.execute(MEASURED, (run_id,)).fetchone()
        if run is None:
            raise ValueError(f"run {run_id} is not imported")
        run_key, measured_at, product_id, ips, requests = run
        common = {
            "product_id": product_id,
            "country": country,
            "run_key": run_key,
            "measured_at": measured_at,
            "ips": ips,
            "measured_requests": requests,
            "formula_version": formula["formula_version"],
        }
        found = hours(formula, country, measured_at, ips, requests)
        rows = [common | row for row in found if row["estimated"]]
        written = 0
        for row in rows:
            written += conn.execute(UPSERT, row).rowcount
    return written
