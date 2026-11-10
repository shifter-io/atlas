"""SIM-GEN-1: the size and statistical checks of generated hours against the profile (SIMULATE.md,
"Checks"). Each returns {"pass", "compared", "checks"}: every check with its relative deviation
(or points, or a distance), its limit and its verdict, never an absolute figure."""

from __future__ import annotations

from collections import Counter, defaultdict

from profile_browser import spread
from profile_hours import bucket
from simulate_browser import dig, outcomes, page_load

MEAN = 0.10  # a generated mean within 10% of the reference's
TVD = 0.05  # total variation distance of a distribution
RECURRENCE_POINTS = 5
OUTCOME_POINTS = 2
PAGE_LOAD = 0.10
ATTRIBUTES = {"country": ("country_code",), "asn": ("asn",)}
ATTRIBUTES["city"] = ("country_code", "region", "city")


def relative(found, expected) -> float:
    if found == expected:
        return 0.0
    return round(found / expected - 1, 6) if expected else 1.0


def tvd(counts: Counter, shares: dict) -> float:
    """The total variation distance of counts from shares (both normalised)."""
    total, whole = sum(counts.values()), sum(shares.values())
    if not total or not whole:
        return 1.0
    names = set(counts) | set(shares)
    return round(sum(abs(counts[k] / total - shares.get(k, 0) / whole) for k in names) / 2, 6)


def verdict(checks: list[dict]) -> dict:
    """Pass when something was compared and every comparison passed."""
    passed = bool(checks) and all(check["pass"] for check in checks)
    return {"pass": passed, "compared": len(checks), "checks": checks}


def size(profile: dict, hours) -> dict:
    """Rows per table and bytes per run, per run kind x network (x target and engine), against
    the reference and the profile's spreads; and each day's rows against reference x runs."""
    groups, days = defaultdict(list), defaultdict(Counter)
    for hour in hours:
        for test in hour.daily:
            figures = test.tables()
            groups["daily", test.lane.network, None, None].append(figures)
            days["daily", hour.day].update(figures | {"runs": 1})
        for run in hour.browser:
            loads = sum(size for _, size, *_ in run.tries) / len(run.tries)
            figures = run.tables() | {"tries": len(run.tries), "bytes_per_load": loads}
            groups["browser", run.network, run.target, run.engine].append(
                figures | {"bundle_bytes": run.bundle_bytes}
            )
            days["browser", hour.day].update(run.tables() | {"runs": 1})
    checks = []
    for (kind, network, target, engine), runs in sorted(groups.items(), key=str):
        group = {"kind": kind, "network": network, "target": target, "engine": engine}
        for metric, expected in references(profile, kind, network, target, engine).items():
            if metric in runs[0] and expected.get("mean") is not None:
                checks.append(band(group, metric, [run[metric] for run in runs], expected))
    for (kind, day), rows in sorted(days.items()):
        for table, expected in dig(profile, "reference", kind, "rows_per_run").items():
            if table in rows and expected.get("mean") is not None:
                deviation = relative(rows[table], expected["mean"] * rows["runs"])
                check = {"check": "day_rows", "kind": kind, "day": day, "metric": table}
                checks.append(check | {"deviation": deviation, "limit": MEAN})
                checks[-1]["pass"] = abs(deviation) <= MEAN
    return verdict(checks)


def references(profile: dict, kind: str, network: str, target, engine) -> dict:
    """metric -> the spread a group's runs are compared with."""
    own = dig(profile, "reference", kind, "networks", network, "rows_per_run")
    found = dict(own or dig(profile, "reference", kind, "rows_per_run"))
    if kind == "browser":
        shape = dig(profile, "networks", network, "browser", target, engine)
        found |= {name: dig(shape, name) for name in ("tries", "bytes_per_load", "bundle_bytes")}
    return found


def band(group: dict, metric: str, values: list, expected: dict) -> dict:
    """The runs' p50 inside the reference's p10-p90 band, their mean within MEAN of its mean."""
    found = spread(values)
    deviation = relative(found["mean"], expected["mean"])
    inside = expected["p10"] <= found["p50"] <= expected["p90"]
    check = group | {"check": "size", "metric": metric, "deviation": deviation, "limit": MEAN}
    return check | {"in_band": inside, "pass": inside and abs(deviation) <= MEAN}


def stats(profile: dict, hours) -> dict:
    """Requests per IP, places, recurrence and browser outcomes and page loads per network."""
    tests, runs = defaultdict(list), defaultdict(list)
    for hour in hours:
        for test in hour.daily:
            tests[test.lane.network].append(test)
        for run in hour.browser:
            runs[run.network, run.target, run.engine].append(run)
    checks = []
    for network, own in sorted(tests.items()):
        checks += daily(dig(profile, "networks", network), network, own)
    for (network, target, engine), own in sorted(runs.items()):
        checks += browser(profile, network, target, engine, own)
    return verdict(checks)


def daily(shape: dict, network: str, tests: list) -> list[dict]:
    seen, places = Counter(), defaultdict(Counter)  # country -> place index -> IPs
    for test in tests:
        for _, _, count, place in test.ips():
            seen[bucket(count)] += 1
            places[test.lane.country][place] += 1
    checks, group = [], {"network": network}
    if shape.get("seen_count_log2"):
        distance = tvd(seen, shape["seen_count_log2"])
        checks.append(group | {"check": "requests_per_ip", "deviation": distance, "limit": TVD})
    measured = {code: own for code, own in places.items() if dig(shape, "places", code)}
    for attribute, fields in ATTRIBUTES.items():
        if measured:
            distance = place_distance(shape, tests[0].model, network, measured, fields)
            checks.append(group | {"check": attribute, "deviation": distance, "limit": TVD})
    later = [test for test in tests if test.day > 1]
    share = dig(shape, "recurrence").get("share")
    if later and share is not None:
        found = sum(test.recurring for test in later) / sum(test.size for test in later)
        points = round(100 * (found - share), 3)
        checks.append(group | {"check": "recurrence", "deviation": points})
        checks[-1]["limit"] = RECURRENCE_POINTS
    return [check | {"pass": abs(check["deviation"]) <= check["limit"]} for check in checks]


def place_distance(shape: dict, model, network: str, measured: dict, fields: tuple) -> float:
    """The IP-weighted mean over the profile's requested countries of one attribute's distance
    from the profile's top tuples, tail and unmatched shares (the model names each pick)."""
    total = weighted = 0
    for code, picks in sorted(measured.items()):
        own, top = dig(shape, "places", code), model.places(network, code).top
        shares, found = Counter(), Counter()
        for place in own.get("top") or []:
            shares[tuple(place.get(name) for name in fields)] += place["share"]
        shares["tail"] += own.get("tail_share") or 0
        shares["unmatched"] += own.get("unmatched_share") or 0
        for place, ips in picks.items():
            if 0 <= place < len(top):
                found[tuple(top[place].get(name) for name in fields)] += ips
            else:
                found["unmatched" if place < 0 else "tail"] += ips
        count = sum(picks.values())
        total, weighted = total + count, weighted + count * tvd(found, shares)
    return round(weighted / total, 6)


def browser(profile: dict, network: str, target: str, engine: str, runs: list) -> list[dict]:
    group = {"network": network, "target": target, "engine": engine}
    expected = outcomes(profile, network, target, engine)
    found = sum((run.outcomes() for run in runs), Counter())
    tries, whole = sum(found.values()), sum(expected.values())
    points = max(abs(found[name] / tries - expected[name] / whole) for name in expected)
    checks = [group | {"check": "outcomes", "deviation": round(100 * points, 3)}]
    checks[-1] |= {"limit": OUTCOME_POINTS, "pass": 100 * points <= OUTCOME_POINTS}
    measured = dig(profile, "reference", "browser", "targets", network, target, engine)
    loads = [ms for run in runs for code, _, ms, _ in run.tries if code is None]
    if loads and dig(measured, "page_load_ms").get("p50"):
        deviation = relative(spread(loads)["p50"], page_load(profile, network, target, engine)[0])
        check = group | {"check": "page_load_p50", "deviation": deviation, "limit": PAGE_LOAD}
        checks.append(check | {"pass": abs(deviation) <= PAGE_LOAD})
    return checks
