"""SIM-LOAD-1: simulate.py loads generated days (2 networks × 3 countries × 2 browser targets)
into the test database standing in for staging; it resumes, calculates, reports and refuses.

The test database stands in for staging (target.STAGING is its name); --no-scope keeps the
calculation out of a systemd scope and --pause makes the retry delays short.
"""

import importlib
import json
import pickle
import re
import stat
import sys
from collections import Counter
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from orchestrator_cases import prepare, query

from proxy_index.calculated import store

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "scale"))
simulate = importlib.import_module("simulate")
simulate_steps = importlib.import_module("simulate_steps")
replay_baseline = importlib.import_module("replay_baseline")
target = importlib.import_module("target")
TABLES = importlib.import_module("profile_reference").TABLES
cases = importlib.import_module("test_scale_simulate_model")

pytest_plugins = ["test_storage"]

GO = f"owner-go REPLAY-RUN-1 {datetime.now(UTC).date()}"
FIRST = (datetime.now(UTC) - timedelta(days=4)).date()
COUNTRIES = tuple(cases.SCHEDULE)
SETTINGS = "SELECT key,value FROM settings ORDER BY key"
DAILY = (  # the network and country of a daily test (daily/__init__.py)
    "SELECT p.provider,upper(o.requested_country),t.summary->>'started_at' FROM test_runs t "
    "JOIN scope_stats s USING(scope_id) JOIN products p USING(product_id) "
    "JOIN pool_stats o USING(pool_id) WHERE t.summary->>'test_kind'='daily'"
)
BROWSER = (
    "SELECT p.provider,b.target_id,b.scheduled_for FROM test_runs t JOIN test_batches b "
    "USING(batch_id) JOIN scope_stats s USING(scope_id) JOIN products p USING(product_id) "
    "WHERE b.kind='browser'"
)
OWN = "SELECT count(*) FROM {} WHERE run_key IN (SELECT run_key FROM test_runs WHERE {})"
MINE = "SELECT run_id FROM test_runs WHERE {}"
SNAPSHOT = "SELECT payload FROM calculated_snapshots WHERE build_id=%s"
PRODUCTION = ROOT / "configs" / "browser-pulse.production.example.toml"  # dict inputs
BROWSER_DATA = ("test_runs", "run_buckets", "test_batches", "batch_runs", "test_events")


@pytest.fixture
def inputs(tmp_path):
    """The formulas, the queue settings and the profile (with a reference) of the cases."""
    formulas = tmp_path / "formulas"
    formulas.mkdir()
    for name in cases.NETWORKS:
        (formulas / f"{name}.json").write_text(json.dumps(cases.formula(name)))
    settings = tmp_path / "browser.toml"
    settings.write_text(cases.SETTINGS)
    given = cases.profile()
    week = cases.week(given, cases.plan(tmp_path), days=(1,))
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(cases.with_reference(given, week)))
    return {"formulas": formulas, "browser-settings": settings, "profile": profile}


def staged(database, monkeypatch):
    """The database as staging: seeded, the cases' countries enabled, the calculated layer."""
    prepare(database, COUNTRIES)
    store.initialize(database)
    monkeypatch.setattr(target, "STAGING", query(database, "SELECT current_database()")[0][0])
    return database


@pytest.fixture
def staging(database, monkeypatch):
    return staged(database, monkeypatch)


def argv(database, inputs, out, days=2, *extra):
    found = ["--database", str(database), "--owner-go", GO, "--days", str(days)]
    found += ["--first-day", FIRST.isoformat(), "--out", str(out), "--jobs", "2", "--seed", "7"]
    found += [f"--{key}={value}" for key, value in inputs.items()]
    return found + ["--pause", "0.01", "--no-scope", *extra]


def state(out):
    return json.loads((out / "state.json").read_text())


def day_of(value) -> date:
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return moment.astimezone(UTC).date()


def daily_figures(found: dict, day: int | None = None) -> list:
    """The recorded daily tests [network, rows, unique IPs, requests] (of a 0-based day)."""
    hours = [h for h in found["hours"] if "figures" in h and day in (None, h["day"])]
    return [test for hour in hours for test in hour["figures"]["daily"]]


def test_two_days_store_the_cadence_and_the_rows_then_publish_a_build(
    staging, inputs, tmp_path, capsys
):
    settings, out = query(staging, SETTINGS), tmp_path / "out"
    assert simulate.main(argv(staging, inputs, out)) == 0
    assert json.loads(capsys.readouterr().out)["hours_done"] == 49
    found = state(out)
    days = (FIRST, FIRST + timedelta(days=1))
    # One daily test per network x country a day, one browser run per network x target a day.
    daily = Counter((n, c, day_of(at)) for n, c, at in query(staging, DAILY))
    assert daily == Counter({(n, c, d): 1 for n in cases.NETWORKS for c in COUNTRIES for d in days})
    browser = Counter((n, t, day_of(at)) for n, t, at in query(staging, BROWSER))
    wanted = {(n, t, d): 1 for n in cases.NETWORKS for t in cases.TARGETS for d in days}
    assert {key: n for key, n in browser.items() if key[2] in days} == wanted
    assert sum(browser.values()) - len(wanted) <= 2  # the extra hour's, at its first minutes
    assert query(staging, "SELECT count(*) FROM test_batches WHERE kind<>'browser'")[0][0] == 0
    # The rows the size check expects of the daily tests, from the recorded figures.
    stored = daily_figures(found)
    marked = "summary->>'test_kind'='daily'"
    for name in ("test_runs", "run_buckets", "ip_observations", "run_enrichments"):
        count = query(staging, OWN.format(name, marked))
        assert count[0][0] == sum(test[1][TABLES.index(name)] for test in stored), name
    estimated = query(staging, OWN.format("estimated_hours", marked))[0][0]
    assert estimated == sum(test[1][TABLES.index("estimated_hours")] for test in stored) > 0
    # Every run carries the run's own id; each planner cell has its two events.
    mine = "SELECT count(*) FILTER (WHERE summary->>'scale_replay'=%s),count(*) FROM test_runs"
    assert len(set(query(staging, mine, (found["replay_id"],))[0])) == 1
    cells = sum(h["ingest"]["steps"]["daily"]["parts"][-1]["cells"] for h in found["hours"])
    events = "SELECT kind,count(*) FROM test_events WHERE kind LIKE 'daily_test_%%' GROUP BY 1"
    assert dict(query(staging, events)) == {
        "daily_test_started": cells,
        "daily_test_finished": cells,
    }
    # state.json records each batch of daily tests as its connection commits: each test once.
    steps = [h["ingest"]["steps"]["daily"] for h in found["hours"]]
    recorded = [run for step in steps for group in step.get("groups", []) for run in group]
    assert sorted(recorded) == sorted(str(run) for (run,) in query(staging, MINE.format(marked)))
    # The full calculation after the days, the incremental one of the extra hour.
    full, incremental = (h["calculate"] for h in found["hours"][-2:])
    assert (full["kind"], incremental["kind"]) == ("full", "incremental")
    assert full["done"] and incremental["done"] and full["build_id"] < incremental["build_id"]
    current = query(staging, "SELECT build_id FROM current_calculation")[0][0]
    assert current == incremental["build_id"]
    # The published build's 24h window (to the extra hour's end; the full build is retired)
    # holds day 2's daily tests and its released hours.
    payload = query(staging, SNAPSHOT, (current,))[0][0]
    networks = {entry["provider"]: entry["windows"]["24h"] for entry in payload["networks"]}
    for network in cases.NETWORKS:  # the network figures leave the browser rows out
        requests = sum(test[3] for test in daily_figures(found, 1) if test[0] == network)
        assert networks[network]["metrics"]["completed"] == requests > 0
        assert networks[network]["estimated"]["hours"] > 0
    # Nodes disabled, no settings row written, the generated hours deleted.
    nodes = query(staging, "SELECT node_id,host,token_hash FROM nodes ORDER BY 1")
    assert sorted(found["nodes"]) == [node[0] for node in nodes]
    assert {host for _, host, _ in nodes} == {"browser-1"}
    assert all(token.startswith("disabled:") for *_, token in nodes)
    assert query(staging, SETTINGS) == settings and not (out / "generated").exists()
    # The report: per day, the sizes against the reference, the calculation, the checks.
    report = json.loads((out / "report.json").read_text())
    assert [d["runs"]["daily"] for d in report["days"]] == [6, 6]
    assert [d["runs"]["browser"] for d in report["days"]] == [4, 4]
    for day in report["days"]:
        runs = day["tables"]["public.test_runs"]
        assert runs["growth"]["rows"] == 10 and runs["expected"]["rows"] == 10
        assert runs["growth"]["table_bytes"] > 0 and day["rows_per_second"] > 0
        assert {row["metric"] for row in day["real_24h"]} >= {"unique IPs", "browser runs"}
        assert set(day["seconds"]) == {"daily", "browser", "release"}
    builds = report["final"]["tables"]["public.calculation_builds"]  # the full one is retired
    assert (
        builds["growth"]["rows"] == 1 and "public.calculated_snapshots" in report["final"]["tables"]
    )
    assert [c["kind"] for c in report["calculation"]] == ["full", "incremental"]
    assert all(c["seconds"] > 0 and c["peak_rss_kb"] for c in report["calculation"])
    checks = report["checks"]
    assert checks["size"]["compared"] and checks["stats"]["compared"]
    assert {c.get("network") for c in checks["stats"]["checks"]} >= set(cases.NETWORKS)
    for name in ("report.json", "report.md", "state.json"):
        assert stat.S_IMODE((out / name).stat().st_mode) == 0o600
    text = (out / "report.md").read_text() + (out / "report.json").read_text()
    assert "Size report, day 2" in text and "## Checks" in text
    assert str(tmp_path) not in text and "browser-1" not in text
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b|2001:db8", text)


def test_a_stopped_load_resumes_to_the_rows_of_an_uninterrupted_one(
    databases, database_template, inputs, tmp_path, monkeypatch, capsys
):
    real, stops = simulate_steps.SimSteps.release, []

    def stop_once(self, day, start, step):
        if start.hour == 5 and not stops:
            stops.append(start)
            raise KeyboardInterrupt
        return real(self, day, start, step)

    stopped = staged(databases(template=database_template), monkeypatch)
    monkeypatch.setattr(simulate_steps.SimSteps, "release", stop_once)
    out = tmp_path / "stopped"
    short = ("--hours-per-day", "8")  # hours 0-7: a daily test and two browser runs a network
    assert simulate.main(argv(stopped, inputs, out, 1, *short)) == 130
    first = state(out)
    assert [h["ingest"]["done"] for h in first["hours"][:6]] == [True] * 5 + [False]
    tokens = query(stopped, "SELECT token_hash FROM nodes")
    assert tokens and all(token.startswith("disabled:") for (token,) in tokens)
    assert (out / "report.md").exists()
    other = argv(stopped, inputs, out, 2, *short)
    assert simulate.main(other) == 2 and "is the load of" in capsys.readouterr().err
    assert simulate.main(argv(stopped, inputs, out, 1, *short)) == 0
    resumed = state(out)
    assert resumed["replay_id"] == first["replay_id"] and len(resumed["resumed"]) == 1
    uninterrupted = staged(databases(template=database_template), monkeypatch)
    assert simulate.main(argv(uninterrupted, inputs, tmp_path / "whole", 1, *short)) == 0
    with simulate.storage.connect(stopped) as a, simulate.storage.connect(uninterrupted) as b:
        mine, whole = replay_baseline.counts(a), replay_baseline.counts(b)
    data = ("test_runs", "run_buckets", "ip_observations", "run_enrichments", "estimated_hours")
    data += ("ip_addresses", "product_ips", "pool_ips", "ip_enrichment", "test_batches")
    data += ("batch_runs", "test_events", "calculation_builds", "calculated_windows")
    assert {name: mine[f"public.{name}"] for name in data} == {
        name: whole[f"public.{name}"] for name in data
    }
    assert mine["public.test_runs"] > 2 and mine["public.estimated_hours"] > 0


def test_pulses_only_when_switched_on(staging, inputs, tmp_path):
    out = tmp_path / "out"
    assert simulate.main(argv(staging, inputs, out, 1, "--pulses", "--hours-per-day", "2")) == 0
    found = state(out)
    pulses = [h["ingest"]["steps"]["pulses"]["parts"][-1] for h in found["hours"]]
    assert all(part["failed"] == 0 for part in pulses) and found["hours"][0]["runs"]["pulse"]
    runs = (
        "SELECT count(*) FROM test_runs JOIN test_batches b USING(batch_id) WHERE b.kind<>'browser'"
    )
    assert query(staging, runs)[0][0] == sum(part["planned"] for part in pulses) > 0
    geo = "SELECT count(*) FROM run_geo_buckets"
    assert query(staging, geo)[0][0] > 0


def test_plan_only_prints_each_day_and_writes_nothing(staging, inputs, tmp_path, capsys):
    before = query(staging, "SELECT count(*) FROM test_runs")
    plan = argv(staging, inputs, tmp_path / "out", 2, "--plan-only")
    assert simulate.main(plan) == 0
    text = capsys.readouterr().out
    assert "day 1: daily 6, browser 4" in text and "day 2: daily 6, browser 4" in text
    assert "total of 2 days: daily 12, browser 8" in text and "pulse" not in text
    assert re.search(r"ip_observations\s+\d+\s+\d+\.\d{3}\s+\d+\.\d{3}", text)
    assert simulate.main([*plan, "--pulses"]) == 0
    assert "pulse " in capsys.readouterr().out
    assert query(staging, "SELECT count(*) FROM test_runs") == before
    assert not (tmp_path / "out").exists()


def test_refusals_write_nothing(staging, inputs, tmp_path, monkeypatch, capsys):
    out = tmp_path / "out"
    later = argv(staging, inputs, out, 7)
    later[later.index("--first-day") + 1] = datetime.now(UTC).date().isoformat()
    assert simulate.main(later) == 2 and "not before now" in capsys.readouterr().err
    bad = argv(staging, inputs, out, 1)
    bad[bad.index("--owner-go") + 1] = "owner-go REPLAY-RUN-1"
    assert simulate.main(bad) == 2 and "is not" in capsys.readouterr().err
    monkeypatch.setattr(target, "PRODUCTION", target.STAGING)
    assert simulate.main(argv(staging, inputs, out, 1)) == 2
    assert "production's" in capsys.readouterr().err
    without = argv(staging, inputs, out, 1)
    del without[without.index("--owner-go") : without.index("--owner-go") + 2]
    with pytest.raises(SystemExit) as exited:
        simulate.main(without)
    assert exited.value.code == 2 and "--owner-go and --out are required" in capsys.readouterr().err
    assert not (out / "state.json").exists()
    assert query(staging, "SELECT count(*) FROM test_runs")[0][0] == 0


def test_the_workers_plan_pickles_with_the_production_settings(tmp_path):
    shared = simulate_steps.SettingsFile.of(PRODUCTION)
    plan = replace(cases.plan(tmp_path), browser=shared, regions=None)
    simulate_steps.start(cases.profile(), pickle.loads(pickle.dumps(plan)), 7, "run")
    targets = simulate_steps.WORKER["model"].plan.browser.targets
    assert {"instagram_web", "cloudflare_challenge"} <= {item.id for item in targets}


def test_a_worker_refuses_a_settings_file_changed_since_the_plan(tmp_path):
    path = tmp_path / "browser.toml"
    path.write_bytes(PRODUCTION.read_bytes())
    plan = replace(cases.plan(tmp_path), browser=simulate_steps.SettingsFile.of(path))
    path.write_text(path.read_text().replace("tries = 100", "tries = 99", 1))
    with pytest.raises(simulate_steps.SettingsChanged, match="browser.toml changed since"):
        simulate_steps.start(cases.profile(), plan, 7, "run")


def test_production_settings_load_the_same_rows_with_one_or_two_jobs(
    databases, database_template, inputs, tmp_path, monkeypatch
):
    given, found = inputs | {"browser-settings": PRODUCTION}, []
    for jobs in ("1", "2"):
        loaded = staged(databases(template=database_template), monkeypatch)
        hour = ("--hours-per-day", "1", "--jobs", jobs)  # one hour, then the extra one
        assert simulate.main(argv(loaded, given, tmp_path / jobs, 1, *hour)) == 0
        assert query(loaded, BROWSER)
        with simulate.storage.connect(loaded) as conn:
            found.append(replay_baseline.counts(conn))
    one, two = ({name: rows[f"public.{name}"] for name in BROWSER_DATA} for rows in found)
    assert one == two and one["test_runs"] > 0
