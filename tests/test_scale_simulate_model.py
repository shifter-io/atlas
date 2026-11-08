"""SIM-GEN-1: simulate_model generates production's cadence and sizes from a synthetic profile
(2 networks, 3 countries, 2 browser targets); simulate_check passes it and fails a shifted one."""

from __future__ import annotations

import copy
import importlib
import ipaddress
import json
import sys
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from orchestrator_cases import prepare, query

from proxy_index.browser.engines import DEFAULT_ENGINE
from proxy_index.estimates import compute
from proxy_index.orchestrator import browser_ingest
from proxy_index.regions import load_regions
from proxy_index.storage import ingest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "scale"))
model = importlib.import_module("simulate_model")
check = importlib.import_module("simulate_check")
spread = importlib.import_module("profile_browser").spread

pytest_plugins = ["test_storage"]

START = date(2026, 11, 2)
NETWORKS = ("alpha", "beta")
SCHEDULE = {"US": 1, "DE": 19, "JP": 13}  # JP: not requested in the profile
TARGETS = ("google_search", "amazon_pdp")
SETTINGS = """schema = 2
enabled = true

[schedule]
countries = ["US"]
lead_seconds = 60
plan = "queue"
queue_period_minutes = 1440

[browser]
browsers_per_host = 2
recycle_after = 1
page_timeout_s = 45
headless = true
block_resources = ["image", "media", "font"]
screenshots = "failures"
locale = "en-US"

[outbox]
dir = "outbox"
max_pending_gb = 20

[[target]]
id = "google_search"
tries = 100
every_minutes = 1440
browser_seconds = 1350
inputs = ["example query"]

[[target]]
id = "amazon_pdp"
tries = 100
every_minutes = 1440
browser_seconds = 1450
inputs = ["0306406152"]

[[host]]
name = "browser-1"
networks = ["alpha/residential", "beta/residential"]
"""
REGIONS = load_regions()


def formula(network: str) -> dict:
    """decodo's formula (committed), as `network`, on SCHEDULE's countries at a 50th of its
    size; beta lacks JP, so JP takes its 10th-percentile country."""
    found = json.loads((ROOT / "formulas" / "decodo.json").read_text()) | {"network": network}
    found["schedule"] = dict(SCHEDULE)
    for entry in found["countries"].values():
        entry["unique_ips"] = [round(n / 50) if n else n for n in entry["unique_ips"]]
    if network == "beta":
        del found["countries"]["JP"]
    return found


def places(code: str, rotate: int = 0) -> dict:
    shares = [0.30, 0.20, 0.15, 0.10, 0.10, 0.05]
    shares = shares[rotate:] + shares[:rotate]
    top = [
        {"country_code": code if i < 5 else "NL", "region": f"R{i % 3}", "city": f"City {i}"}
        | {"asn": 64500 + i % 4, "usage_type": "consumer", "asn_name": f"Org {i % 4}"}
        | {"latitude": 1.5, "longitude": 2.5, "share": share}
        for i, share in enumerate(shares)
    ]
    return {"ips": 5000, "top": top, "tail_share": 0.095, "unmatched_share": 0.005}


def browser_entry(ok: int, bundle: int) -> dict:
    outcomes = dict.fromkeys(("login_wall", "no_content", "no_answer", "other_error"))
    outcomes |= {"ok": ok, "challenge": 90 - ok, "block": 2, "timeout": 5, "proxy_error": 0}
    return {
        "runs": 3,
        "tries": spread([100, 100, 100]),
        "outcomes": outcomes | {"unsplit": 3},
        "bytes_per_load": spread([1_400_000, 1_500_000, 1_600_000]),
        "bundle_bytes": spread([bundle * 4 // 5, bundle, bundle * 6 // 5]),
    }


def profile(shift: float = 1.0) -> dict:
    """The synthetic profile; shift != 1 moves sizes, shapes, recurrence and outcomes."""
    networks = {}
    for name in NETWORKS:
        hours = [{"hour": f"{h:02d}", "successes": 950, "errors": 50} for h in range(24)]
        hours = [hour | {"countries": {}} for hour in hours]
        hours[1]["countries"]["US"] = {"unique_ips": round(1500 * shift), "request_rows": 4000}
        hours[19]["countries"]["DE"] = {"unique_ips": round(900 * shift), "request_rows": 2000}
        seen = {"1": 600, "2-3": 250, "4-7": 100, "8-15": 50}
        if shift != 1:
            seen = {"1": 100, "2-3": 150, "4-7": 300, "8-15": 450}
        rotate = 0 if shift == 1 else 2
        networks[name] = {
            "hours": hours,
            "seen_count_log2": seen,
            "recurrence": {"share": 0.45 if shift == 1 else 0.7},
            "places": {code: places(code, rotate) for code in ("US", "DE")},
            "browser": {
                "google_search": {DEFAULT_ENGINE: browser_entry(round(40 * shift), 300_000)},
                "amazon_pdp": {DEFAULT_ENGINE: browser_entry(round(60 * shift), 500_000)},
            },
        }
    loads = {"p50": round(3000 * shift), "p90": round(6000 * shift)}
    outcomes = dict.fromkeys(("block", "login_wall", "proxy_error", "no_answer", "other_error"), 0)
    outcomes |= {"ok": round(50 * shift), "challenge": 100 - round(50 * shift)}
    outcomes |= {"timeout": 0, "no_content": 0}
    targets = {"alpha": {"google_search": {DEFAULT_ENGINE: {"outcomes": outcomes}}}}
    targets["alpha"]["google_search"][DEFAULT_ENGINE]["page_load_ms"] = loads
    reference = {"browser": {"targets": targets}}
    return {"profile_version": 1, "networks": networks, "reference": reference}


def plan(tmp_path, **options) -> model.Plan:
    path = tmp_path / "browser-settings.toml"
    path.write_text(SETTINGS)
    settings = model.load_settings(path)
    formulas = {name: formula(name) for name in NETWORKS}
    return model.Plan(formulas, settings, START, regions=REGIONS, **options)


def week(given: dict, built: model.Plan, seed=1, days=range(1, 8)) -> list:
    found = model.Model(given, built, seed)
    return [found.hour(day, h) for day in days for h in range(24)]


def with_reference(given: dict, hours: list) -> dict:
    """The profile with a reference of these generated hours' rows per run (by kind and
    network), as profile.py reference reads one from a database."""
    runs = {"daily": [], "browser": []}
    for hour in hours:
        runs["daily"] += [(test.lane.network, test.tables()) for test in hour.daily]
        runs["browser"] += [(run.network, run.tables()) for run in hour.browser]
    reference = copy.deepcopy(given["reference"])
    for kind, found in runs.items():
        tables = {name for _, figures in found for name in figures}
        entry = reference.setdefault(kind, {})
        entry["rows_per_run"] = {t: spread([f[t] for _, f in found]) for t in tables}
        mine = {name: [f for n, f in found if n == name] for name in NETWORKS}
        entry["networks"] = {
            name: {"rows_per_run": {t: spread([f[t] for f in own]) for t in tables}}
            for name, own in mine.items()
        }
    return given | {"reference": reference}


def test_one_daily_test_per_network_and_country_and_one_browser_run_per_target_a_day(tmp_path):
    hours = week(profile(), plan(tmp_path), days=(1, 2))
    for day in (1, 2):
        own = [hour for hour in hours if hour.day == day]
        tests = Counter((t.lane.network, t.lane.country) for hour in own for t in hour.daily)
        assert tests == Counter({(n, c): 1 for n in NETWORKS for c in SCHEDULE})
        runs = Counter((run.network, run.target) for hour in own for run in hour.browser)
        assert runs == Counter({(n, t): 1 for n in NETWORKS for t in TARGETS})
        assert not any(hour.pulses for hour in own)
    for hour in hours:
        for test in hour.daily:
            assert test.started_at.hour == SCHEDULE[test.lane.country] == hour.start.hour
        for run in hour.browser:
            assert hour.start <= run.start < hour.start + timedelta(hours=1)
            assert len(run.tries) == 100 and run.engine == DEFAULT_ENGINE
    one = next(hour for hour in hours if hour.daily)
    (report,) = [r for r in one.reports() if r["network"] == "alpha"]
    assert report["region"] == "nyc3" and report["countries"] == ["US"]
    assert report["unique_ips"] == one.daily[0].size and report["requests"] > report["unique_ips"]


def test_sizes_follow_the_profile_then_the_formula(tmp_path):
    built = plan(tmp_path)
    found = model.Model(profile(), built, 3)
    jp = {name: found.lanes[name, "JP"].ips for name in NETWORKS}
    assert found.lanes["alpha", "US"].ips == 1500 and found.lanes["beta", "DE"].ips == 900
    assert jp["alpha"] == formula("alpha")["countries"]["JP"]["unique_ips"][SCHEDULE["JP"]]
    assert jp["beta"] == formula("beta")["countries"]["DE"]["unique_ips"][SCHEDULE["DE"]]
    sizes = [found.daily(found.lanes["alpha", "US"], day).size for day in range(1, 8)]
    assert len(set(sizes)) == 7 and all(1350 <= size <= 1650 for size in sizes)


def test_hours_are_deterministic_alone_or_together(tmp_path):
    built, given = plan(tmp_path), profile()
    alone = model.hour(given, built, 5, 2, 1)
    together = week(given, built, 5, days=(2,))[1]
    assert alone == together and alone.daily
    for found, name in ((alone, "a"), (together, "b")):
        (tmp_path / name).mkdir()
        found.daily[0].write(tmp_path / name)
    for file in ("summary.json", "ips.jsonl.gz"):
        assert (tmp_path / "a" / file).read_bytes() == (tmp_path / "b" / file).read_bytes()
    assert model.hour(given, built, 6, 2, 1).daily[0] != alone.daily[0]


def test_a_days_ips_recur_from_the_day_before_at_the_profile_share(tmp_path):
    built = plan(tmp_path)
    found = model.Model(profile(), built, 7)
    lane = found.lanes["alpha", "US"]
    days = [{n for n, *_ in found.daily(lane, day).ips()} for day in (1, 2, 3)]
    for before, after in zip(days, days[1:], strict=False):
        assert len(before & after) / len(after) == pytest.approx(0.45, abs=0.002)
    other = {n for n, *_ in found.daily(found.lanes["beta", "US"], 1).ips()}
    assert not other & days[0]  # each lane keeps its own block
    reserved = (ipaddress.ip_network((0xF0000000, 4)), ipaddress.ip_network("2001:db8::/32"))
    for n in days[0]:  # class E (240/4) or the IPv6 documentation prefix
        assert any(ipaddress.ip_address(model.address(n)) in block for block in reserved)


def test_pulses_only_when_switched_on(tmp_path):
    assert not model.hour(profile(), plan(tmp_path, countries=("US", "DE")), 1, 1, 4).pulses
    built = plan(tmp_path, countries=("US", "DE"), pulses=(("generic_http", 30),))
    pulses = model.hour(profile(), built, 1, 1, 4).pulses
    assert len(pulses) == 2 * len(NETWORKS) * 2  # two 30-minute slots
    for pulse in pulses:
        assert sum(completed for _, completed, *_ in pulse.geo) == 100
        assert pulse.start.minute in (0, 30)


def test_both_checks_pass_on_generated_days_and_fail_on_shifted_ones(tmp_path):
    built = plan(tmp_path)
    given = with_reference(profile(), week(profile(), built, 1))
    generated = week(given, built, 2)
    sized, stated = check.size(given, generated), check.stats(given, generated[24:48])
    assert sized["pass"], [c for c in sized["checks"] if not c["pass"]]
    assert stated["pass"], [c for c in stated["checks"] if not c["pass"]]
    assert {c["check"] for c in stated["checks"]} >= {"requests_per_ip", "country", "asn", "city"}
    assert {"recurrence", "outcomes", "page_load_p50"} <= {c["check"] for c in stated["checks"]}
    allowed = {"check", "kind", "network", "target", "engine", "day", "metric", "deviation"}
    allowed |= {"limit", "in_band", "pass"}  # deviations and verdicts, no absolute figure
    assert all(set(c) <= allowed for c in sized["checks"] + stated["checks"])
    shifted = week(profile(1.4), built, 2)
    assert not check.size(given, shifted)["pass"]
    failed = {c["check"] for c in check.stats(given, shifted[24:48])["checks"] if not c["pass"]}
    assert {"requests_per_ip", "country", "recurrence", "outcomes", "page_load_p50"} <= failed


def test_nothing_to_compare_does_not_pass(tmp_path):
    hours = week({}, plan(tmp_path), days=(1,))
    assert check.size({}, hours) == {"pass": False, "compared": 0, "checks": []}


def test_production_cadence_from_the_formulas_and_the_production_queue():
    built = model.Plan(
        {f.stem: json.loads(f.read_text()) for f in (ROOT / "formulas").glob("*.json")},
        model.load_settings(model.BROWSER_SETTINGS),
        START,
        regions=REGIONS,
    )
    found = model.Model({}, built, 1)
    hours = [found.hour(1, h) for h in range(24)]
    assert sum(len(hour.daily) for hour in hours) == 3848
    assert max(len(hour.daily) for hour in hours) == len(hours[13].daily) == 442
    assert sum(len(hour.browser) for hour in hours) == 144 == 18 * 8
    assert not any(hour.pulses for hour in hours)


def test_a_daily_test_is_ingested_with_the_rows_it_counts(database, tmp_path):
    prepare(database, tuple(SCHEDULE))
    hour = model.hour(profile(), plan(tmp_path), 9, 2, SCHEDULE["DE"])
    (test,) = [t for t in hour.daily if t.lane.network == "alpha"]
    directory = tmp_path / "run"
    directory.mkdir()
    test.write(directory)
    assert ingest(directory, database)["imported"]
    compute.estimate_run(database, test.run_id, "DE", test.lane.formula)
    (run_key,) = query(database, "SELECT run_key FROM test_runs")[0]
    expected = test.tables()
    for table in ("run_buckets", "ip_observations", "run_enrichments", "estimated_hours"):
        found = query(database, f"SELECT count(*) FROM {table} WHERE run_key=%s", (run_key,))
        assert found[0][0] == expected[table], table
    for table in ("ip_addresses", "ip_enrichment", "product_ips", "pool_ips"):
        assert query(database, f"SELECT count(*) FROM {table}")[0][0] == expected[table], table
    summary = json.loads((directory / "summary.json").read_text())
    assert summary["unique_ips"] == test.size and summary["test_kind"] == "daily"


def test_a_browser_run_is_a_bundle_the_ingest_accepts(tmp_path):
    hours = week(profile(), plan(tmp_path), days=(1,))
    run = next(r for hour in hours for r in hour.browser if r.target == "amazon_pdp")
    start = run.start.astimezone(UTC)
    row = {"batch_id": 4, "product_id": 1, "country": run.country, "provider": run.network}
    row |= {"product": run.product, "scheduled_for": start, "start_at": start}
    path = run.bundle(row, 1, tmp_path / "outbox")
    assert abs(path.stat().st_size / run.bundle_bytes - 1) < 0.1
    work = tmp_path / "work"
    work.mkdir()
    path.replace(work / "bundle.tar.gz")
    summary, manifest = browser_ingest._open_bundle(work, 1 << 30)
    state = {"target_id": "amazon_pdp", "batch_id": 4, "country_code": "US"}
    node = {"provider": run.network, "product": run.product}
    browser_ingest._check(summary, manifest, summary["run_id"], state, node)
    lines = (work / "browser" / "tries.jsonl").read_text().splitlines()
    tries = [json.loads(line) for line in lines]
    assert Counter(t["error"] for t in tries) == Counter(code for code, *_ in run.tries)
    shots = sorted((work / "browser" / "screenshots").glob("*.jpg"))
    assert len(shots) == sum(1 for code, *_ in run.tries if code)
    assert summary["completed"] == 100 and datetime.fromisoformat(summary["started_at"]) >= start


def test_the_plan_is_read_once_from_the_database(database, tmp_path):
    prepare(database, ("US", "DE"))
    for name in NETWORKS:
        (tmp_path / f"{name}.json").write_text(json.dumps(formula(name)))
    built = model.load_plan(database, START, tmp_path)
    assert built.countries == ("DE", "US") and set(built.formulas) == set(NETWORKS)
    assert not built.pulses  # production has no pulse fleet: none unless switched on
    pulsed = model.load_plan(database, START, tmp_path, pulses=True).pulses
    assert pulsed and {minutes for _, minutes in pulsed} == {30}
    assert built.browser.schedule.plan == "queue" and len(built.browser.hosts) == 3
    slots = ROOT / "configs" / "browser-pulse.example.toml"
    with pytest.raises(ValueError, match="schedule.plan"):
        model.load_plan(database, START, tmp_path, slots)
    with pytest.raises(ValueError, match="day must be"):
        built.day_start(8)
