"""The released estimated hours in the calculator windows (24h, 7d, 30d).

Each product window whose cohort holds the daily tests gains an `estimated` block, built only
from estimated_hours rows released by the cutoff whose hour starts inside the window; the
measured figures of the window are not changed. Every figure in the block says it is estimated
and carries its formula version:

- requests: the sum of the released hours' estimated requests (hours never overlap);
- unique_ips: by `window_unique()` over the window's counted tests (`counted()`). The default,
  "distinct", counts the exact distinct IPs those measured tests saw (the tests of that product
  window, so of its cohort only) and scales them by their day factor (day unique IPs over
  measured IPs), so an IP seen on several days counts once; "sum" adds each test's day unique
  IPs instead (counts it every day). The methodology's `[estimates] window_unique` picks one.
  A test's figure is a whole day's, so the 24h window counts each country's most recent test
  only: the previous day's test, whose later hours reach into the window, is another day
  (UNIQUE-IPS-24H-1, #1117). The 7d and 30d windows count every test, one per day;
- countries and series: the same per requested country (its counted tests) and per UTC hour
  (every released hour);
- breakdown_scale: estimated over measured unique IPs. City, region, ASN and country shares stay
  the measured breakdowns'; multiply a breakdown's IPs by it for an estimate;
- carried: overlap and datacenter ASN shares and latency are the measured window's, unchanged.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from ..calculated.hourly import window_distinct

WINDOWS = {"24h": 24, "7d": 168, "30d": 720}
METHODS = ("distinct", "sum")
HOUR_START = "(day + make_interval(hours => hour::int)) AT TIME ZONE 'UTC'"

# The day bounds only let the estimated_hours_day index narrow the scan.
RELEASED = f"""SELECT product_id,country_code,day::text AS day,hour,run_key,measured_ips,
 unique_ips,requests,day_unique_ips,formula_version,{HOUR_START} AS hour_start
FROM estimated_hours WHERE day>=(%(start)s AT TIME ZONE 'UTC')::date
 AND day<=(%(as_of)s AT TIME ZONE 'UTC')::date AND released_at<=%(as_of)s
 AND {HOUR_START}>=%(start)s AND {HOUR_START}<%(as_of)s
ORDER BY hour_start,product_id,country_code"""


def fetch(conn, as_of) -> list[dict]:
    """The released hours of the longest window."""
    start = as_of - timedelta(hours=max(WINDOWS.values()))
    return conn.execute(RELEASED, {"as_of": as_of, "start": start}).fetchall()


def counted(window: dict, rows: list[dict]) -> list[dict]:
    """The released hours (rows) of the tests whose IPs the window's unique IPs count: in a 24h
    window each country's most recent test, the one holding its latest released hour (an hour
    stays with the latest test that covers it; on a tie the larger run key), else every one."""
    if window["period"].get("hours") != 24:
        return rows
    newest = {}
    for r in rows:
        key = (r["hour_start"], r["run_key"])
        if r["country_code"] not in newest or key > newest[r["country_code"]]:
            newest[r["country_code"]] = key
    return [r for r in rows if r["run_key"] == newest[r["country_code"]][1]]


def distinct(conn, found: list[tuple[dict, list[dict]]]) -> list[dict]:
    """For each (window, hours) of found, the distinct measured IPs of its counted tests
    (counted()) by country (None: all of them), counted once per set of tests
    (hourly.window_distinct())."""
    run_sets = [{(r["run_key"], r["country_code"]) for r in counted(*f)} for f in found]
    return window_distinct(conn, run_sets, True)


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
    tests = counted(window, rows)
    by_country, tests_of = defaultdict(list), defaultdict(list)
    for r in rows:
        by_country[r["country_code"]].append(r)
    for r in tests:
        tests_of[r["country_code"]].append(r)
    countries = [
        {
            "country_code": code,
            "unique_ips": window_unique(method, tests_of[code], distinct.get(code, 0)),
            "requests": sum(r["requests"] for r in group),
            "estimated": True,
            "formula_version": max(r["formula_version"] for r in group),
        }
        for code, group in sorted(by_country.items())
    ]
    unique = window_unique(method, tests, distinct.get(None, 0))
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


def windows_with(groups: dict, run_group: dict, hours: list[dict]) -> list:
    """Every product window with released hours in it, as (window, those hours).

    A test's hours go to the product group its run was calculated in (run_group[run_key]).
    """
    rows_of = defaultdict(list)
    for r in hours:
        if r["run_key"] in run_group:
            rows_of[run_group[r["run_key"]]].append(r)
    found = []
    for key, rows in rows_of.items():
        for window in groups[key]["windows"].values():
            inside = [r for r in rows if r["hour_start"] >= window["period"]["start"]]
            if inside:
                found.append((window, inside))
    return found


def place(found: list, counts: list[dict], config: dict) -> None:
    """Add the `estimated` block to each found window; counts are distinct()'s."""
    method = config.get("estimates", {}).get("window_unique", "distinct")
    for (window, inside), ips in zip(found, counts, strict=True):
        window["estimated"] = block(window, inside, ips, method)


def attach(payload: dict, runs: list, conn, as_of, config: dict) -> dict:
    """The built payload with the released estimated hours placed in its product windows.

    build() records each run's product group (run["group"], the (product_id, cohort) key).
    """
    groups = {(p["product_id"], p["cohort"]): p for p in payload["products"]}
    run_group = {r["run_key"]: r["group"] for r in runs if "group" in r}
    found = windows_with(groups, run_group, fetch(conn, as_of))
    place(found, distinct(conn, found), config)
    return payload
