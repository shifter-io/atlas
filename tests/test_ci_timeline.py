"""The opt-in test timeline: helpers.timed writes it, scripts/ci/timeline.py sums it."""

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import conftest
import helpers
import pytest
from test_ci_timing import steps

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "timeline.py"


def phase(worker, nodeid, kind, start, stop):
    return {"worker": worker, "nodeid": nodeid, "kind": kind, "start": start, "stop": stop}


def write(directory, worker, records):
    lines = "".join(json.dumps(record) + "\n" for record in records)
    (directory / f"timeline-{worker}.jsonl").write_text(lines)


def test_timed_records_only_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("EV", str(tmp_path))
    monkeypatch.setattr(helpers, "TIMELINE", False)
    with helpers.timed("create_database"):
        pass
    assert list(tmp_path.iterdir()) == []
    monkeypatch.setattr(helpers, "TIMELINE", True)
    with helpers.timed("create_database"):
        pass
    (path,) = tmp_path.iterdir()
    assert path.name == f"timeline-{helpers.WORKER}.jsonl"
    (record,) = map(json.loads, path.read_text().splitlines())
    assert record["kind"] == "create_database" and record["worker"] == helpers.WORKER
    assert record["start"] <= record["stop"]


def test_timeline_sums_busy_idle_spans_and_longest(tmp_path):
    write(
        tmp_path,
        "gw0",
        [
            {"worker": "gw0", "kind": "sessionstart", "pid": 1},
            phase("gw0", "a", "setup", 100.0, 101.0),
            phase("gw0", "a", "call", 101.0, 104.0),
            phase("gw0", "a", "teardown", 104.0, 104.5),
            {"worker": "gw0", "kind": "create_database", "start": 100.2, "stop": 100.4},
            {"worker": "gw0", "kind": "initialize", "start": 101.0, "stop": 101.5},
        ],
    )
    write(tmp_path, "gw1", [phase("gw1", "b", "call", 100.0, 110.0)])
    timeline = runpy.run_path(str(SCRIPT))
    result = timeline["summarize"](timeline["load"](tmp_path), top=1)
    assert result["wall"] == 10.0
    assert result["workers"]["gw0"] == {"busy": 4.5, "tests": 1, "idle": 5.5}
    assert result["workers"]["gw1"] == {"busy": 10.0, "tests": 1, "idle": 0.0}
    assert result["spans"]["create_database"] == pytest.approx(0.2)
    assert result["spans"]["initialize"] == 0.5
    assert result["longest"] == [("b", 10.0, 1.0)]

    out = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path), "--top", "2"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.startswith("wall 10.0 s\ngw0: busy 4.5 s, idle 5.5 s, 1 tests\n")
    assert "busy total 14.5 s over 2 workers" in out
    assert "initialize 0.5 s (3.4% of busy)" in out
    assert out.rstrip().endswith("4.5 s  45.0% of wall  a")


def test_timeline_reports_when_each_worker_ended(tmp_path):
    write(tmp_path, "gw0", [phase("gw0", "a", "call", 100.0, 103.0)])
    write(
        tmp_path,
        "gw1",
        [phase("gw1", "b", "setup", 101.0, 102.0), phase("gw1", "b", "call", 102.0, 108.5)],
    )
    timeline = runpy.run_path(str(SCRIPT))
    result = timeline["summarize"](timeline["load"](tmp_path))
    assert result["ends"] == {"gw0": 3.0, "gw1": 8.5}
    assert "\nworkers end 3.0-8.5 s: gw0 3.0, gw1 8.5\n" in timeline["report"](result)
    assert timeline["summarize"]([])["ends"] == {}


def test_timeline_reports_the_fixed_costs_around_the_tests(tmp_path):
    scope = {"name": "scope", "user": 40.0, "sys": 2.5}
    step = [
        {"worker": "step", "kind": "rewrite", "start": 88.0, "stop": 90.0},
        {"worker": "step", "kind": "pytest", "start": 90.0, "stop": 125.0},
        {"worker": "step", "kind": "check", "start": 90.0, "stop": 150.0} | scope,
    ]
    write(tmp_path, "step", step)
    write(
        tmp_path,
        "main",
        [
            {"worker": "main", "kind": "combine", "start": 121.0, "stop": 121.5},
            {"worker": "main", "kind": "xml_report", "start": 121.5, "stop": 124.0},
        ],
    )
    write(
        tmp_path,
        "gw0",
        [
            {"worker": "gw0", "kind": "collect", "start": 92.0, "stop": 98.0},
            phase("gw0", "a", "call", 100.0, 120.0),
        ],
    )
    write(tmp_path, "gw1", [{"worker": "gw1", "kind": "collect", "start": 93.5, "stop": 99.0}])
    timeline = runpy.run_path(str(SCRIPT))
    fixed = timeline["summarize"](timeline["load"](tmp_path))["fixed"]
    assert fixed["before"] == 10.0 and fixed["after"] == 5.0
    assert fixed["collect"] == (2.0, 3.5, 8.0, 9.0)
    assert fixed["coverage"] == {"combine": 0.5, "xml_report": 2.5}
    assert fixed["checks"] == {"scope": {"wall": 60.0, "cpu": 42.5, "end": 50.0}}
    assert fixed["step"] == {"rewrite": 2.0}
    assert timeline["fixed_report"](fixed) == [
        "before pytest: rewrite 2.0 s",
        "pytest start to first test 10.0 s, last test to end 5.0 s",
        "workers loaded 2.0-3.5 s, collected 8.0-9.0 s after pytest start",
        "coverage: combine 0.5 s, xml_report 2.5 s",
        "check scope: wall 60.0 s, cpu 42.5 s, ends at 50.0 s",
    ]
    # Without tests: the coverage and the checks, no start or end.
    empty = timeline["summarize"](step)["fixed"]
    assert "before" not in empty and empty["checks"]["scope"]["end"] is None
    assert timeline["fixed_report"](empty) == [
        "before pytest: rewrite 2.0 s",
        "check scope: wall 60.0 s, cpu 42.5 s",
    ]


def test_timeline_reports_the_processes_starts_and_the_database_peak(tmp_path):
    write(tmp_path, "step", [{"worker": "step", "kind": "pytest", "start": 90.0, "stop": 125.0}])
    collect = {"kind": "collect", "start": 93.0, "stop": 97.0}
    write(
        tmp_path,
        "main",
        [{"worker": "main", "kind": "controller", "start": 90.5, "stop": 91.0}],
    )
    write(
        tmp_path,
        "gw0",
        [
            {"worker": "gw0", "process": 92.0} | collect,
            phase("gw0", "a", "call", 100.0, 120.0),
            {"worker": "gw0", "kind": "databases", "count": 40, "start": 120.0},
        ],
    )
    write(
        tmp_path,
        "gw1",
        [
            {"worker": "gw1", "process": 92.5} | collect | {"stop": 98.0},
            {"worker": "gw1", "kind": "databases", "count": 55, "start": 121.0},
        ],
    )
    timeline = runpy.run_path(str(SCRIPT))
    fixed = timeline["summarize"](timeline["load"](tmp_path))["fixed"]
    assert fixed["controller"] == (0.5, 1.0) and fixed["started"] == (2.0, 2.5)
    assert fixed["databases"] == 55
    assert timeline["fixed_report"](fixed) == [
        "pytest start to first test 10.0 s, last test to end 5.0 s",
        "controller started 0.5 s, loaded 1.0 s after pytest start",
        "workers started 2.0-2.5 s, loaded 3.0-3.0 s, collected 7.0-8.0 s after pytest start",
        "databases: peak 55 (sampled before each drop)",
    ]
    # Without the processes' starts (not Linux): the line as before.
    write(tmp_path, "main", [{"worker": "main", "kind": "controller", "start": None, "stop": 1}])
    write(tmp_path, "gw1", [])
    unknown = {"worker": "gw0", "process": None} | collect
    write(tmp_path, "gw0", [unknown, phase("gw0", "a", "call", 100.0, 120.0)])
    fixed = timeline["summarize"](timeline["load"](tmp_path))["fixed"]
    assert "controller" not in fixed and "started" not in fixed and "databases" not in fixed
    lines = timeline["fixed_report"](fixed)
    assert "workers loaded 3.0-3.0 s, collected 7.0-7.0 s after pytest start" in lines


def test_timeline_of_no_tests(tmp_path):
    out = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path)], capture_output=True, text=True, check=True
    ).stdout
    assert out == "wall 0.0 s\nbusy total 0.0 s over 0 workers\n"


def test_durations_keep_the_tests_of_at_least_min_seconds(tmp_path):
    write(
        tmp_path,
        "gw0",
        [
            phase("gw0", "slow", "setup", 0.0, 0.5),
            phase("gw0", "slow", "call", 0.5, 2.04),
            phase("gw0", "fast", "call", 2.04, 2.5),
        ],
    )
    out = tmp_path / "durations.json"
    timeline = runpy.run_path(str(SCRIPT))
    assert timeline["main"]([str(tmp_path), "--durations", str(out), "--min", "1"]) == 0
    assert json.loads(out.read_text()) == {"slow": 2.0}


class Item:
    def __init__(self, nodeid):
        self.nodeid = nodeid

    def __repr__(self):
        return self.nodeid


def test_chunks_follow_the_worksteal_first_split():
    assert conftest.chunks(10, 3) == [3, 3, 4]
    assert conftest.chunks(2, 3) == [0, 1, 1]
    assert sum(conftest.chunks(2958, 32)) == 2958


def test_spread_gives_each_chunk_its_share_of_slow_tests_longest_first():
    items = [Item(name) for name in "abcdefghijkl"]
    durations = {"b": 50.0, "c": 40.0, "d": 30.0, "e": 20.0, "k": 25.0, "x": 99.0}
    ordered = [item.nodeid for item in conftest.spread(items, durations, 3)]
    assert sorted(ordered) == list("abcdefghijkl")
    # Chunks of 4: the longest to the least loaded chunk with room, then fast tests in order.
    assert ordered == ["b", "a", "f", "g", "c", "e", "h", "i", "d", "k", "j", "l"]


def test_spread_with_more_slow_tests_than_a_chunk_holds():
    items = [Item(name) for name in "abcd"]
    ordered = conftest.spread(items, {"a": 9.0, "b": 8.0, "c": 7.0, "d": 6.0}, 2)
    assert [item.nodeid for item in ordered] == ["a", "d", "b", "c"]


class Config:
    def __init__(self, workerinput=None):
        if workerinput is not None:
            self.workerinput = workerinput


def test_only_xdist_workers_reorder(monkeypatch, tmp_path):
    durations = tmp_path / "durations.json"
    durations.write_text(json.dumps({"b": 5.0}))
    monkeypatch.setattr(conftest, "DURATIONS", durations)
    items = [Item("a"), Item("b")]
    conftest.pytest_collection_modifyitems(Config(), items)
    assert [item.nodeid for item in items] == ["a", "b"]
    items = [Item("a"), Item("b")]
    conftest.pytest_collection_modifyitems(Config({"workercount": 2}), items)
    assert [item.nodeid for item in items] == ["b", "a"]
    monkeypatch.setattr(conftest, "DURATIONS", tmp_path / "missing.json")
    conftest.pytest_collection_modifyitems(Config({"workercount": 2}), items := [Item("a")])
    assert [item.nodeid for item in items] == ["a"]


def run_with_timeline(ev, *args, **env):
    """Run one fast test of this file in a fresh pytest with CI_TIMELINE=1 and EV=ev (without
    xdist unless `args` ask for it)."""
    inherited = ("PYTEST_XDIST", "CI_TIMELINE", "COV_CORE", "COVERAGE")
    environment = {k: v for k, v in os.environ.items() if not k.startswith(inherited)}
    environment.update(CI_TIMELINE="1", EV=str(ev), **env)
    nodeid = "tests/test_ci_timeline.py::test_chunks_follow_the_worksteal_first_split"
    subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", nodeid]
        + list(args or ("-p", "no:xdist")),
        cwd=SCRIPT.parents[2],
        env=environment,
        capture_output=True,
        check=True,
    )
    return nodeid


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_the_timeline_records_each_phase_of_the_test_process(tmp_path):
    nodeid = run_with_timeline(tmp_path)
    (path,) = tmp_path.iterdir()
    assert path.name == "timeline-main.jsonl"
    collect, *phases = records(path)
    assert collect["kind"] == "collect" and collect["start"] <= collect["stop"]
    assert [(r["kind"], r["nodeid"], r["outcome"]) for r in phases] == [
        (phase, nodeid, "passed") for phase in ("setup", "call", "teardown")
    ]
    assert all(r["worker"] == "main" and r["start"] <= r["stop"] for r in phases)
    assert collect["stop"] <= phases[0]["start"]


def test_the_xdist_controller_records_only_its_coverage_spans(tmp_path):
    report = tmp_path / "reports"
    run_with_timeline(
        tmp_path,
        "-n",
        "1",
        "--cov=atlas",
        f"--cov-report=json:{report / 'coverage.json'}",
        COVERAGE_FILE=str(report / ".coverage"),
    )
    started, *controller = records(tmp_path / "timeline-main.jsonl")
    assert [r["kind"] for r in controller] == ["combine", "report", "json_report"]
    assert all(r["worker"] == "main" and r["start"] <= r["stop"] for r in controller)
    # When the controller started (Linux only) and loaded conftest.py.
    assert started["kind"] == "controller" and started["stop"] <= controller[0]["start"]
    assert started["start"] is None or started["start"] <= started["stop"]
    worker = [r["kind"] for r in records(tmp_path / "timeline-gw0.jsonl")]
    assert worker == ["collect", "setup", "call", "teardown"]


def test_a_pytest_started_by_a_recording_test_does_not_record(tmp_path):
    # pid 1 is never the nested pytest: it inherited another process's ownership.
    run_with_timeline(tmp_path, CI_TIMELINE_OWNER="1")
    assert list(tmp_path.iterdir()) == []


def test_tier1_writes_the_coverage_reports_after_pytest_and_runs_its_checks_at_nice_19():
    run = next(s for s in steps("ci.yml", "tier1") if s.get("id") == "tests")["run"]
    pytest_runs = [line for line in run.splitlines() if "pytest -p no:cacheprovider -rs" in line]
    assert len(pytest_runs) == 2
    # pytest only combines the coverage; both reports the gates read come after the last run.
    assert run.count("--cov-report= ") == 2 and "--cov-report=xml" not in run
    serial = run.index('--junitxml="$EV/junit-serial.xml"')
    for kind in ("xml", "json"):
        report = f'coverage {kind} -q -o "$EV/coverage.{kind}"'
        assert serial < run.index(report) < run.index("\nwait\n")
    assert 'renice -n 19 -p "$BASHPID"' in run
    assert 'span pytest "$start"' in run and 'span check "$start"' in run


def test_only_the_first_xdist_worker_traces_its_collection(monkeypatch):
    def tracer(*args):
        return tracer

    traced = []
    for worker in ("main", "gw0", "gw7"):
        monkeypatch.setattr(helpers, "WORKER", worker)
        monkeypatch.setattr(sys, "gettrace", lambda: tracer)
        settrace = []
        monkeypatch.setattr(sys, "settrace", settrace.append)
        hook = conftest.pytest_collection(None)
        next(hook)
        with pytest.raises(StopIteration) as done:
            hook.send("collected")
        assert done.value.value == "collected"
        traced.append(settrace)
    # gw7 collects without a tracer and gets it back after; the others keep it throughout.
    assert traced == [[], [], [None, tracer]]


def test_no_test_patches_a_modules_file():
    # Coverage names a file by its module's __file__ when it first traces it: in a worker that
    # collected untraced, that can be inside the test that patched it (pytest_collection).
    tests = Path(__file__).parent
    patched = [
        path.name
        for path in tests.glob("*.py")
        if path.name != Path(__file__).name and '"__file__",' in path.read_text()
    ]
    assert patched == []
