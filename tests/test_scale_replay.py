"""REPLAY-1: the replay of simulated days in production's database, on a fixture slice set.

The test database stands in for production (target.PRODUCTION is its name); --no-scope keeps
the calculation out of a systemd scope and --pause makes the retry delays short.
"""

import gzip
import importlib
import json
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiohttp
import psycopg
import pytest
from orchestrator_cases import prepare, query

from proxy_index.calculated import store
from proxy_index.storage import connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "scale"))
replay = importlib.import_module("replay")
replay_steps = importlib.import_module("replay_steps")
synthetic = importlib.import_module("synthetic")
target = importlib.import_module("target")
browser_model = importlib.import_module("browser_model")
test_scale_cycle = importlib.import_module("test_scale_cycle")

pytest_plugins = ["test_storage"]

NETWORK = "decodo"  # a committed formula
COUNTRIES = ("US", "DE")
RELEASE = "b" * 64
GO = f"owner-go REPLAY-RUN-1 {datetime.now(UTC).date()}"
FIRST = (datetime.now(UTC) - timedelta(days=3)).date().isoformat()
SETTINGS = "SELECT key,value FROM settings ORDER BY key"
DAILY = "SELECT run_key,run_id::text FROM test_runs WHERE summary->>'test_kind'='daily' ORDER BY 1"


def place(code, i):
    return {
        "matched": True,
        "country_code": code,
        "country": f"Country {code}",
        "region_code": "R1",
        "region": "Region 1",
        "city": f"City {i % 3}",
        "latitude": 10.0 + i % 3,
        "longitude": 20.0 + i % 3,
        "isp": f"ISP {i % 2}",
        "organization": f"ISP {i % 2}",
        "asn": 64500 + i % 2,
        "asn_organization": f"Network {i % 2}",
        "connection_type": "Cable/DSL",
        "usage_type": "residential",
    }


def write_slices(directory, hours=(0, 1, 2), ips=6):
    """slice-00..23: a daily test per country in each of `hours`, synthetic addresses only."""
    directory.mkdir()
    n = 0
    for hour in range(24):
        tests, rows = [], []
        for code in COUNTRIES if hour in hours else ():
            mine = []
            for i in range(ips):
                row = {"network": NETWORK, "country": code, "ip": synthetic.address(n)}
                row |= {"count": 1 + i % 3, "enrichment": place(code, i), "modelled": False}
                mine.append(row | {"dbip_release": RELEASE})
                n += 1
            rows += mine
            requests = sum(row["count"] for row in mine)
            tests.append({"network": NETWORK, "country": code, "best_hour": hour})
            tests[-1] |= {"unique_ips": ips, "request_rows": requests, "slot_errors": 2}
        (directory / f"slice-{hour:02d}.json").write_text(json.dumps({"tests": tests}))
        lines = "".join(json.dumps(row) + "\n" for row in rows).encode()
        (directory / f"slice-{hour:02d}.jsonl.gz").write_bytes(gzip.compress(lines))
    return directory


@pytest.fixture
def production(database, monkeypatch):
    """The test database as production: seeded, US and DE enabled, the calculated layer."""
    prepare(database, COUNTRIES)
    store.initialize(database)
    (name,) = query(database, "SELECT current_database()")[0]
    monkeypatch.setattr(target, "PRODUCTION", name)
    return database


def argv(database, tmp_path, days=2, hours=3, *extra):
    slices = tmp_path / "slices"
    if not slices.exists():
        write_slices(slices)
    found = ["--database", str(database), "--owner-go", GO, "--slices", str(slices)]
    found += ["--days", str(days), "--first-day", FIRST, "--out", str(tmp_path / "out")]
    return found + ["--hours-per-day", str(hours), "--pause", "0.01", "--no-scope", *extra]


def state(tmp_path):
    return json.loads((tmp_path / "out" / "state.json").read_text())


def events(tmp_path):
    path = tmp_path / "out" / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def moment(text):
    return datetime.fromisoformat(text)


def test_two_days_of_three_hours_overlap_in_order_with_markers_and_sizes(
    production, tmp_path, capsys
):
    settings = query(production, SETTINGS)
    assert replay.main(argv(production, tmp_path)) == 0
    assert json.loads(capsys.readouterr().out)["hours_done"] == 6
    found = state(tmp_path)
    hours = found["hours"]
    assert [h["start"][:13] for h in hours[:4]] == [
        f"{FIRST}T00",
        f"{FIRST}T01",
        f"{FIRST}T02",
        f"{(datetime.fromisoformat(FIRST) + timedelta(days=1)).date()}T00",
    ]
    assert len(hours) == 6 and all(h["calculate"]["done"] and h["ingest"]["done"] for h in hours)
    for now, later in zip(hours, hours[1:], strict=False):
        assert moment(now["ingest"]["finished"]) <= moment(now["calculate"]["started"])
        assert moment(later["ingest"]["started"]) < moment(now["calculate"]["finished"])
        assert moment(later["calculate"]["started"]) >= moment(now["calculate"]["finished"])
    for now, after in zip(hours, hours[2:], strict=False):
        assert moment(after["ingest"]["started"]) >= moment(now["calculate"]["finished"])
    for hour in hours:
        assert hour["sizes"]["database_bytes"] > 0
        assert hour["sizes"]["all_databases_bytes"] >= hour["sizes"]["database_bytes"]
        assert hour["ingest"]["statements"] and hour["calculate"]["statements"]
        assert hour["calculate"]["build_id"]
        daily = hour["ingest"]["steps"]["daily"]["parts"][-1]
        assert daily["stored"] == 2 and daily["failed"] == 0
        pulses = hour["ingest"]["steps"]["pulses"]["parts"][-1]
        assert pulses["planned"] > 0 and pulses["failed"] == 0
        assert hour["ingest"]["steps"]["browser"]["parts"][-1]["runs"] == 8
    assert events(tmp_path) == [] and found["counts"]["retries"] == 0
    # The report (REPLAY-REPORT-1), written after every hour.
    report = json.loads((tmp_path / "out" / "report.json").read_text())
    assert len(report["hours"]) == 6 and len(report["days_done"]) == 2
    for hour in report["hours"]:
        # Its daily tests, pulses and browser runs: every test_runs row of the hour is the replay's.
        assert hour["rows"]["public.test_runs"] > 2 and hour["per_test"]["count"] == 2
        assert hour["calculate_seconds"] and hour["slowest"] and hour["growth_bytes"] is not None
    assert all(day["replay_rows"]["public.test_runs"] > 4 for day in report["days_done"])
    assert report["final"]["day"] == 2 and report["final"]["tables_gb"]
    assert [s for s in report["statements"] if s["statement"].startswith("COPY") and s["rows"]]
    assert (tmp_path / "out" / "report.md").exists()
    # The baseline, the markers and the day counts.
    baseline = found["baseline"]
    assert baseline["counts"]["public.test_runs"] == 0
    assert "public.test_runs.run_key" in baseline["identities"]
    assert {"providers", "nodes", "targets", "place_names"} <= set(baseline["keys"])
    marked = "SELECT count(*) FILTER (WHERE summary->>'scale_replay'=%s),count(*) FROM test_runs"
    (mine, total) = query(production, marked, (found["replay_id"],))[0]
    assert mine == total > 6 * 2
    nodes = query(production, "SELECT node_id,host,status,token_hash FROM nodes ORDER BY 1")
    assert sorted(found["nodes"]) == [node[0] for node in nodes]
    for _, host, status, token in nodes:
        assert host.startswith(replay_steps.NODE_HOST) or host == browser_model.HOST
        assert status == "offline" and token.startswith("disabled:")
    assert set(found["days_counted"]) == {"0", "1"}
    day = found["days_counted"]["1"]
    assert day["replay"]["public.test_runs"] == day["counts"]["public.test_runs"] == total
    assert day["other"]["public.test_runs"] == 0 and day["other"]["public.job_runs"] == 0
    assert query(production, SETTINGS) == settings  # no settings row written
    assert found["go"]["go"] == GO and found["database"] == target.PRODUCTION
    assert found["started_at"] == found["go"]["at"] and found["resumed"] == []


def test_a_failed_calculation_pauses_doubling_and_retries_the_same_hour(
    production, tmp_path, monkeypatch, capsys
):
    real, calls = replay.measure.run_child, []

    def failing(command, poll, timeout=None, stop=None):
        calls.append(command)
        if len(calls) == 1:  # over the hour's timeout: killed
            return real([sys.executable, "-c", "import time; time.sleep(30)"], poll, 0.3, stop)
        if len(calls) <= 4:
            return real([sys.executable, "-c", "raise SystemExit(3)"], poll, timeout, stop)
        return real(command, poll, 120, stop)  # the real calculation, not held to 0.3 s

    monkeypatch.setattr(replay.measure, "run_child", failing)
    assert replay.main(argv(production, tmp_path, 1, 1, "--hour-timeout", "0.3")) == 0
    found = events(tmp_path)
    assert [e["step"] for e in found] == ["calculate"] * 4
    assert [e["reason"] for e in found] == ["over 0.3 s"] + ["exit 3"] * 3
    assert [e["pause"] for e in found] == [0.01, 0.02, 0.04, 0.08]
    assert {e["hour"] for e in found} == {f"{FIRST}T00:00:00+00:00"}
    hour = state(tmp_path)["hours"][0]
    assert hour["retries"] == 4 and hour["calculate"]["done"]
    assert [a.get("killed") for a in hour["calculate"]["attempts"]] == ["timeout"] + [None] * 4
    counts = state(tmp_path)["counts"]
    assert counts["retries"] == 4 and counts["calculate"] == 4
    assert counts["pause_seconds"] == pytest.approx(0.15)
    assert "retried 4 times (calculate)" in capsys.readouterr().err
    assert query(production, "SELECT count(*) FROM current_calculation")[0][0] == 1


def test_an_ingest_failure_keeps_the_runs_already_stored(production, tmp_path, monkeypatch, capsys):
    real, calls = replay_steps.storage.ingest, []

    def unwritable(out):
        raise OSError("disk full")

    monkeypatch.setattr(replay.replay_report, "write", unwritable)  # the replay goes on

    def flaky(directory, database, **options):
        if "scale-replay-" in str(directory):  # a daily test, not a pulse the node API ingests
            calls.append(directory)
        if len(calls) == 2 and "scale-replay-" in str(directory):
            raise ValueError("forced")
        return real(directory, database, **options)

    monkeypatch.setattr(replay_steps.storage, "ingest", flaky)
    assert replay.main(argv(production, tmp_path, 1, 1)) == 0
    [event] = events(tmp_path)
    assert event["step"] == "daily" and "forced" in event["reason"] and event["pause"] == 0.01
    parts = state(tmp_path)["hours"][0]["ingest"]["steps"]["daily"]["parts"]
    assert [(p["stored"], p["failed"]) for p in parts] == [(1, 1), (2, 0)]
    assert len(calls) == 3  # the retry ingests only the missing run
    stored = query(production, DAILY)
    daily = state(tmp_path)["hours"][0]["ingest"]["steps"]["daily"]
    assert len(stored) == 2 and sorted(run_id for _, run_id in stored) == sorted(daily["ingested"])
    assert "no report after hour" in capsys.readouterr().err


def test_a_failed_estimate_and_browser_hour_are_retried_and_their_nodes_kept(
    production, tmp_path, monkeypatch
):
    estimate, drive, calls = replay_steps.compute.estimate_run, browser_model.Hour.drive, []

    def flaky_estimate(database, run_id, country, formula):
        calls.append(run_id)
        if len(calls) == 1:
            raise psycopg.OperationalError("connection lost")
        return estimate(database, run_id, country, formula)

    async def flaky_drive(self, text, nodes):
        if not getattr(flaky_drive, "failed", False):  # after the nodes are registered
            flaky_drive.failed = True
            raise aiohttp.ClientError("register refused")
        return await drive(self, text, nodes)

    monkeypatch.setattr(replay_steps.compute, "estimate_run", flaky_estimate)
    monkeypatch.setattr(browser_model.Hour, "drive", flaky_drive)
    assert replay.main(argv(production, tmp_path, 1, 1)) == 0
    assert [e["step"] for e in events(tmp_path)] == ["daily", "browser"]
    assert calls[0] in calls[1:]  # the retry estimates the run again
    found = state(tmp_path)
    steps = found["hours"][0]["ingest"]["steps"]
    assert [(p["stored"], p["failed"]) for p in steps["daily"]["parts"]] == [(1, 1), (2, 0)]
    browser = steps["browser"]["parts"][-1]
    assert browser["runs"] == 8 and browser["networks"] == 1
    mine = "SELECT node_id,host,token_hash FROM nodes WHERE host=%s"
    hosts = query(production, mine, (browser_model.HOST,))
    assert len(hosts) == 1 and hosts[0][0] in found["nodes"] and hosts[0][2].startswith("disabled:")


def test_a_stopped_replay_resumes_from_its_state_file(production, tmp_path, monkeypatch, capsys):
    real, stops = replay_steps.ReplaySteps.release, []

    def stop_once(self, day, start, step):
        if start.hour == 1 and not stops:
            stops.append(start)
            raise KeyboardInterrupt
        return real(self, day, start, step)

    monkeypatch.setattr(replay_steps.ReplaySteps, "release", stop_once)
    assert replay.main(argv(production, tmp_path, 1, 2)) == 130
    first = state(tmp_path)
    assert first["hours"][0]["ingest"]["done"] and not first["hours"][0]["calculate"]["done"]
    assert first["hours"][0]["calculate"]["attempts"] == []  # killed while stopping
    tokens = query(production, "SELECT token_hash FROM nodes")
    assert tokens and all(token.startswith("disabled:") for (token,) in tokens)
    path = tmp_path / "out" / "state.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "out").stat().st_mode) == 0o700
    wrong = argv(production, tmp_path, 2, 2)
    assert replay.main(wrong) == 2 and "is the replay of" in capsys.readouterr().err
    with test_scale_cycle.Siteapi(production) as site:
        resumed = argv(production, tmp_path, 1, 2, "--siteapi", site.url)
        assert replay.main(resumed + ["--siteapi-timeout", "20"]) == 0
    found = state(tmp_path)
    assert found["replay_id"] == first["replay_id"] and len(found["resumed"]) == 1
    assert all(h["calculate"]["done"] for h in found["hours"])
    assert found["hours"][1]["calculate"]["siteapi"]["reached"]
    assert len(query(production, DAILY)) == 4  # no run stored twice


def test_refusals_write_nothing(production, tmp_path, capsys):
    out = tmp_path / "out"
    later = (datetime.now(UTC) - timedelta(hours=2)).date().isoformat()
    future = argv(production, tmp_path, 2, 24)
    future[future.index("--first-day") + 1] = later
    assert replay.main(future) == 2 and "not before now" in capsys.readouterr().err
    bad = argv(production, tmp_path, 1, 1)
    bad[bad.index("--owner-go") + 1] = "owner-go REPLAY-RUN-1"
    assert replay.main(bad) == 2 and "is not" in capsys.readouterr().err
    with connect(production) as conn:
        recent = "INSERT INTO job_runs(job,started_at) VALUES ('calculate',now()-'4 min'::interval)"
        conn.execute(recent)
    assert replay.main(argv(production, tmp_path, 1, 1)) == 2
    assert "the orchestrator runs" in capsys.readouterr().err
    assert not (out / "state.json").exists()
    with connect(production) as conn:
        conn.execute("UPDATE job_runs SET started_at=now()-'6 min'::interval")
        # An older row does not refuse.
        assert replay.guard(conn, replay.arguments(argv(production, tmp_path, 1, 1)))["go"] == GO


def test_the_memory_scope_is_used_where_systemd_run_exists(monkeypatch):
    monkeypatch.setattr(replay.shutil, "which", lambda name: f"/usr/bin/{name}")
    scope = replay.memory_scope()
    assert scope[:2] == ["systemd-run", "--scope"] and scope[-1] == "--"
    assert "MemoryMax=10G" in scope and "MemorySwapMax=0" in scope
    assert replay.memory_scope(False) == []
    monkeypatch.setattr(replay.shutil, "which", lambda name: None)
    assert replay.memory_scope() == []
