"""SIM-GEN-1, SIM-CHECK-2: the size and statistical checks of generated hours against the plan
and the profile (SIMULATE.md, "Checks"). Each returns {"pass", "compared", "checks"}: every
check with its relative deviation (or points, or a distance), its limit and its verdict, never
an absolute figure; size adds its notes."""

from __future__ import annotations

from collections import Counter, defaultdict

import replay_report
from profile_browser import spread
from profile_hours import bucket
from simulate_browser import bytes_per_load, dig, outcomes, page_load

MEAN = 0.10  # a generated mean within 10% of the reference's
TVD = 0.05  # total variation distance of a distribution
RECURRENCE_POINTS = 5
OUTCOME_POINTS = 2
PAGE_LOAD = 0.10
ATTRIBUTES = {"country": ("country_code",), "asn": ("asn",)}
ATTRIBUTES["city"] = ("country_code", "region", "city")
TOP_K = 50  # an attribute's distance: over the profile's top 50 values and one tail bucket
REFERENCE_IPS = 5000  # fewer IPs in a network's places: an insufficient reference
APPEND = ("ip_observations", "run_buckets", "test_runs", "estimated_hours", "test_events")
NEW_IPS = ("ip_addresses", "ip_enrichment", "product_ips", "pool_ips", "scope_ips")
INGEST = APPEND + NEW_IPS
# A daily ratio: (rows of one table, rows of the other).
RATIOS = {
    "observations_per_ip": ("ip_observations", "ip_addresses"),
    "enrichment_per_ip": ("ip_enrichment", "ip_addresses"),
    "buckets_per_run": ("run_buckets", "test_runs"),
    **{f"{name}_per_ip": (name, "ip_addresses") for name in NEW_IPS[2:]},
}
SCHEDULES = (
    "BP-VERIFY-3's runs came from the old slot schedule; production's queue runs one run of the "
    "target's tries per network x target a day (docs/api-web-map.md section 14), so BP-VERIFY-3 "
    "is a reference per try only"
)


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


def size(profile: dict, hours, model, sizes: dict | None = None) -> dict:
    """The browser runs against the plan and, per try, BP-VERIFY-3; each network's daily ratios
    and unique IPs; with `sizes` (state.json's, the baseline as "0"), each day's growth
    (SIMULATE.md, "Checks"). The notes beside the checks: a day's browser tries against
    BP-VERIFY-3's, and the rows each day added apart from the ingest's tables."""
    hours = list(hours)
    checks = browser_sizes(profile, hours, model) + ratios(profile, hours)
    checks += unique_ips(hours, model)
    notes = {"browser_tries_day": tries_day(profile, hours)}
    if sizes:
        found, notes["apart"] = day_rows(profile, hours, sizes)
        checks += found
    return verdict(checks) | notes


def snapshots(state: dict) -> dict:
    """state.json's sizes at the end of each day, the baseline's as day "0"."""
    return state["sizes"] | {"0": state["baseline"]["sizes"]}


def within(check: dict, found, expected, limit: float = MEAN) -> dict:
    deviation = relative(found, expected)
    return check | {"deviation": deviation, "limit": limit, "pass": abs(deviation) <= limit}


def browser_sizes(profile: dict, hours: list, model) -> list[dict]:
    """Per network x target x engine: tries per run against the plan's, and the runs' p50
    bytes per page load against the profile's (simulate_browser.bytes_per_load, its basis). A
    run is the model's or a stored one (`loads`, `bytes_per_load`)."""
    groups = defaultdict(list)
    for hour in hours:
        for run in hour.browser:
            groups[run.network, run.target, run.engine].append(run)
    planned = {item.id: item.tries for item in model.plan.browser.targets}
    checks = []
    for (network, target, engine), runs in sorted(groups.items()):
        group = {"kind": "browser", "network": network, "target": target, "engine": engine}
        tries = sum(run.loads for run in runs) / len(runs)
        checks.append(within(group | {"check": "tries_per_run"}, tries, planned[target]))
        loads = [run.bytes_per_load for run in runs]
        expected, basis = bytes_per_load(profile, network, target, engine)
        found = within(group | {"check": "bytes_per_load_p50"}, spread(loads)["p50"], expected)
        checks.append(found | {"basis": basis})
    return checks


def tries_day(profile: dict, hours: list) -> dict:
    """A day's browser tries, generated (the mean over the days), beside BP-VERIFY-3's."""
    days = Counter()
    for hour in hours:
        days[hour.day] += sum(run.loads for run in hour.browser)
    real = sum(entry.get("browser_tries") or 0 for entry in dig(profile, "real_24h").values())
    generated = round(sum(days.values()) / len(days)) if days else 0
    return {"generated": generated, "bp_verify_3": real, "schedules": SCHEDULES}


def reference_ratio(profile: dict, network: str, top: str, bottom: str):
    """The reference's mean `top` rows over its mean `bottom` rows per daily run: the network's
    runs, else all; None without both."""
    rows = dig(profile, "reference", "daily", "networks", network, "rows_per_run")
    rows = rows or dig(profile, "reference", "daily", "rows_per_run")
    over, under = dig(rows, top).get("mean"), dig(rows, bottom).get("mean")
    return over / under if over is not None and under else None


def ratios(profile: dict, hours: list) -> list[dict]:
    """Per network, its daily tests' RATIOS against the reference's."""
    rows = defaultdict(Counter)
    for hour in hours:
        for test in hour.daily:
            rows[test.lane.network].update(test.tables())
    checks = []
    for network, own in sorted(rows.items()):
        for name, (top, bottom) in RATIOS.items():
            expected = reference_ratio(profile, network, top, bottom)
            if expected is not None and top in own and own[bottom]:
                check = {"kind": "daily", "network": network, "check": "ratio", "metric": name}
                checks.append(within(check, own[top] / own[bottom], expected))
    return checks


def unique_ips(hours: list, model) -> list[dict]:
    """Per network and day, its tests' unique IPs against their formulas' figures, each varied
    by the day's jitter as the model varies a test's size."""
    found, planned = Counter(), Counter()
    for hour in hours:
        for test in hour.daily:
            lane = test.lane
            factor = model.jitter("size", lane.network, lane.country, hour.day)
            found[lane.network, hour.day] += test.size
            planned[lane.network, hour.day] += max(1, round(lane.planned * factor))
    return [
        within({"kind": "daily", "network": n, "day": d, "check": "unique_ips"}, found[n, d], p)
        for (n, d), p in sorted(planned.items())
    ]


def day_rows(profile: dict, hours: list, sizes: dict) -> tuple[list[dict], dict]:
    """Each stored day's growth of the ingest's tables against expected_rows; and per day the
    rows added apart: other tables', and a table's on the day it appeared (a migration's
    back-fill)."""
    checks, apart = [], {}
    for day in sorted({hour.day for hour in hours}):
        before, after = sizes.get(str(day - 1)), sizes.get(str(day))
        if not (before and after):
            continue
        grew, apart[str(day)] = growth(before, after)
        expected = expected_rows(profile, [hour for hour in hours if hour.day == day], day)
        for name, rows in sorted(expected.items()):
            if name in grew:
                check = {"check": "day_rows", "day": day, "metric": name}
                checks.append(within(check, grew[name], rows))
    return checks, apart


def growth(before: dict, after: dict) -> tuple[dict, dict]:
    """(ingest table -> rows added, table -> the rows added apart and why)."""
    grew, apart = {}, {}
    for name, now in sorted(after["tables"].items()):
        table, was = name.removeprefix("public."), before["tables"].get(name)
        added = now["rows"] - (was or {}).get("rows", 0)
        if table in INGEST and was is not None:
            grew[table] = added
        elif added:
            why = "not the ingest's" if table not in INGEST else "new table: a back-fill"
            apart[table] = {"rows": added, "why": why}
    return grew, apart


def expected_rows(profile: dict, hours: list, day: int) -> dict:
    """A day's rows per ingest table. Append-only: the daily IPs x the reference's observations
    per IP, else the runs x the reference's rows per run of their kind. New-IP tables: the new
    IPs (day 1 all, later days the share not recurring) x the reference's rows per IP (1 for a
    table it lacks)."""
    runs, ips, new = Counter(), Counter(), Counter()
    for hour in hours:
        runs.update({"daily": len(hour.daily), "browser": len(hour.browser)})
        for test in hour.daily:
            ips[test.lane.network] += test.size
            new[test.lane.network] += test.size * (1 if day == 1 else 1 - test.lane.reuse)
    found = {}
    for name in INGEST:
        if name == "ip_observations" or name in NEW_IPS:
            own = new if name in NEW_IPS else ips
            given = [(n, reference_ratio(profile, n, name, "ip_addresses")) for n in own]
            if name in NEW_IPS:
                given = [(n, 1 if ratio is None else ratio) for n, ratio in given]
            if all(ratio is not None for _, ratio in given):
                found[name] = round(sum(own[n] * ratio for n, ratio in given))
            continue
        means = {kind: dig(profile, "reference", kind, "rows_per_run", name) for kind in runs}
        if all(mean.get("mean") is not None for mean in means.values()):
            found[name] = round(sum(n * means[kind]["mean"] for kind, n in runs.items()))
    return found


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
    known = sum(dig(shape, "places", code).get("ips") or 0 for code in measured)
    for attribute, fields in ATTRIBUTES.items():
        if measured:
            distance = place_distance(shape, tests[0].model, network, measured, fields)
            checks.append(group | {"check": attribute, "deviation": distance, "limit": TVD})
            checks[-1] |= {"insufficient_reference": True} if known < REFERENCE_IPS else {}
    later = [test for test in tests if test.day > 1]
    share = dig(shape, "recurrence").get("share")
    if later and share is not None:
        found = sum(test.recurring for test in later) / sum(test.size for test in later)
        points = round(100 * (found - share), 3)
        checks.append(group | {"check": "recurrence", "deviation": points})
        checks[-1]["limit"] = RECURRENCE_POINTS
    for check in checks:  # an insufficient reference is reported, never failed
        inside = abs(check["deviation"]) <= check["limit"]
        check["pass"] = inside or bool(check.get("insufficient_reference"))
    return checks


def place_distance(shape: dict, model, network: str, measured: dict, fields: tuple) -> float:
    """The IP-weighted mean over the profile's requested countries of one attribute's distance
    from the profile's shares, on the same support for both: the attribute's TOP_K values with
    the most share, one tail bucket for every other value, and unmatched (the model names each
    pick)."""
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
        named = [k for k in shares if isinstance(k, tuple)]
        named.sort(key=lambda k: (-shares[k], repr(k)))
        kept = set(named[:TOP_K])
        count, found, shares = sum(picks.values()), folded(found, kept), folded(shares, kept)
        total, weighted = total + count, weighted + count * tvd(found, shares)
    return round(weighted / total, 6)


def folded(counts: Counter, kept: set) -> Counter:
    """counts with every value outside `kept` in the tail bucket (unmatched stays apart)."""
    found = Counter()
    for name, n in counts.items():
        found[name if name in kept or name == "unmatched" else "tail"] += n
    return found


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


def label(check: dict) -> str:
    keys = ("kind", "network", "target", "engine", "day")
    return " ".join(str(check[key]) for key in keys if check.get(key) is not None)


def markdown(found: dict) -> str:
    """checks.md: per check its failures, the insufficient references, a day's browser tries
    beside BP-VERIFY-3's and the rows added apart from the ingest's tables."""
    lines = [f"# Checks of the stored load (SIM-CHECK-2), stats of day {found['stats_day']}"]
    head = ("Check", "Group", "Metric", "Deviation", "Limit")
    for name in ("size", "stats"):
        own = found[name]
        failed = [c for c in own["checks"] if not c["pass"]]
        lines += ["", f"## {name}: {own['compared'] - len(failed)} of {own['compared']} pass"]
        rows = [[c["check"], label(c), c.get("metric"), c["deviation"], c["limit"]] for c in failed]
        lines += replay_report.table(head, rows) if rows else ["", "No failure."]
        thin = [c for c in own["checks"] if c.get("insufficient_reference")]
        lines += [""] if thin else []
        lines += [f"- insufficient reference: {label(c)} {c['check']}" for c in thin]
    tries = found["size"]["browser_tries_day"]
    lines += ["", "## Browser tries a day", "", f"Generated {tries['generated']}, BP-VERIFY-3 "]
    lines[-1] += f"{tries['bp_verify_3']}: {tries['schedules']}."
    rows = [
        [day, name, entry["rows"], entry["why"]]
        for day, tables in sorted(found["size"].get("apart", {}).items())
        for name, entry in tables.items()
    ]
    lines += ["", "## Rows apart from the ingest's tables"]
    lines += replay_report.table(("Day", "Table", "Rows", "Why"), rows)
    return "\n".join(lines) + "\n"
