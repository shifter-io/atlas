"""The released estimated hours in the calculator windows (24h, 7d, 30d).

Each product window whose cohort holds the daily tests gains an `estimated` block, built only
from estimated_hours rows released by the cutoff whose hour starts inside the window; the
measured figures of the window are not changed. Every figure in the block says it is estimated
and carries its formula version:

- requests: the sum of the released hours' estimated requests (hours never overlap);
- unique_ips: by `window_unique()`. The default, "distinct", counts the exact distinct IPs the
  window's measured tests saw and scales them by those tests' day factor (day unique IPs over
  measured IPs), so an IP seen on several days counts once; "sum" adds each test's day unique
  IPs instead (counts it every day). The methodology's `[estimates] window_unique` picks one;
- countries and series: the same per requested country and per UTC hour;
- breakdown_scale: estimated over measured unique IPs. City, region, ASN and country shares stay
  the measured breakdowns'; multiply a breakdown's IPs by it for an estimate;
- carried: overlap and datacenter ASN shares and latency are the measured window's, unchanged.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

WINDOWS = {"24h": 24, "7d": 168, "30d": 720}
METHODS = ("distinct", "sum")
HOUR_START = "(day + make_interval(hours => hour::int)) AT TIME ZONE 'UTC'"

RELEASED = f"""SELECT product_id,country_code,day::text AS day,hour,run_key,measured_ips,
 unique_ips,requests,day_unique_ips,formula_version,{HOUR_START} AS hour_start
FROM estimated_hours WHERE released_at<=%(as_of)s AND {HOUR_START}>=%(start)s
 AND {HOUR_START}<%(as_of)s ORDER BY hour_start,product_id,country_code"""

# Distinct measured IPs of the tests behind a window's released hours, per country and in all
# (country_code NULL).
DISTINCT = f"""SELECT e.product_id,e.country_code,count(DISTINCT o.ip_id) AS ips
FROM ip_observations o JOIN (SELECT DISTINCT product_id,country_code,run_key FROM estimated_hours
 WHERE released_at<=%(as_of)s AND {HOUR_START}>=%(start)s AND {HOUR_START}<%(as_of)s) e
 USING(run_key)
GROUP BY GROUPING SETS ((e.product_id,e.country_code),(e.product_id)) ORDER BY 1,2"""


def fetch(conn, as_of) -> dict:
    """The released hours of the longest window and each window's distinct measured IPs."""
    start = as_of - timedelta(hours=max(WINDOWS.values()))
    hours = conn.execute(RELEASED, {"as_of": as_of, "start": start}).fetchall()
    distinct = {}
    for label, length in WINDOWS.items():
        bounds = {"as_of": as_of, "start": as_of - timedelta(hours=length)}
        distinct[label] = conn.execute(DISTINCT, bounds).fetchall()
    return {"hours": hours, "distinct": distinct}


def window_unique(method: str, rows: list[dict], distinct_ips: int) -> float:
    """A window's estimated unique IPs from its released hours (rows) of one or more tests."""
    tests = {r["run_key"]: r for r in rows}.values()
    if method == "sum":
        return sum(t["day_unique_ips"] for t in tests)
    if method != "distinct":
        raise ValueError(f"estimates window_unique must be one of {METHODS}, not {method!r}")
    measured = sum(t["measured_ips"] for t in tests)
    return distinct_ips * sum(t["day_unique_ips"] for t in tests) / measured if measured else 0.0


def _figure(value: float, versions: list[int], **extra) -> dict:
    return {"value": value, "estimated": True, "formula_version": max(versions)} | extra


def _series(rows: list[dict]) -> list[dict]:
    hours = defaultdict(list)
    for r in rows:
        hours[r["hour_start"]].append(r)
    return [
        {
            "start": start,
            "unique_ips": sum(r["unique_ips"] for r in group),
            "requests": sum(r["requests"] for r in group),
            "estimated": True,
            "formula_version": max(r["formula_version"] for r in group),
        }
        for start, group in sorted(hours.items())
    ]


def block(window: dict, rows: list[dict], distinct: dict, method: str) -> dict:
    """The `estimated` block of one product window; distinct maps country (None: all) to IPs."""
    versions = sorted({r["formula_version"] for r in rows})
    by_country = defaultdict(list)
    for r in rows:
        by_country[r["country_code"]].append(r)
    countries = [
        {
            "country_code": code,
            "unique_ips": window_unique(method, group, distinct.get(code, 0)),
            "requests": sum(r["requests"] for r in group),
            "estimated": True,
            "formula_version": max(r["formula_version"] for r in group),
        }
        for code, group in sorted(by_country.items())
    ]
    unique = window_unique(method, rows, distinct.get(None, 0))
    measured = window["pool"]["unique_ips"]
    return {
        "estimated": True,
        "formula_versions": versions,
        "hours": len(rows),
        "unique_ips": _figure(unique, versions, method=method),
        "requests": _figure(sum(r["requests"] for r in rows), versions),
        "countries": countries,
        "series": _series(rows),
        "breakdown_scale": unique / measured if measured else None,
        "carried": {
            "shared_share": window["overlap"]["shared_share"],
            "datacenter_asn_share": window["pool"].get("datacenter_asn_share"),
            "latency": "measured",
        },
    }


def attach(groups: dict, run_map: dict, estimates: dict | None, config: dict) -> None:
    """Add the `estimated` block to every product window with released hours in it.

    A test's hours go to the product group its run was calculated in (run_map[run_key]).
    """
    if not estimates or not estimates["hours"]:
        return
    method = config.get("estimates", {}).get("window_unique", "distinct")
    rows_of = defaultdict(list)
    for r in estimates["hours"]:
        if r["run_key"] in run_map:
            rows_of[run_map[r["run_key"]]["group"]].append(r)
    for key, rows in rows_of.items():
        product = groups[key]
        for label, window in product["windows"].items():
            start = window["period"]["start"]
            inside = [r for r in rows if r["hour_start"] >= start]
            if not inside:
                continue
            distinct = {
                d["country_code"]: d["ips"]
                for d in estimates["distinct"].get(label, [])
                if d["product_id"] == product["product_id"]
            }
            window["estimated"] = block(window, inside, distinct, method)
