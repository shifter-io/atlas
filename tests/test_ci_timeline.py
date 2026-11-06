"""The opt-in test timeline: helpers.timed writes it, scripts/ci/timeline.py sums it."""

import json
import runpy
import subprocess
import sys
from pathlib import Path

import helpers
import pytest

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


def test_timeline_of_no_tests(tmp_path):
    out = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path)], capture_output=True, text=True, check=True
    ).stdout
    assert out == "wall 0.0 s\nbusy total 0.0 s over 0 workers\n"
