"""CI time (CI-TIME-1): scripts/ci/timing.py and timing_history.py, run as CI runs them."""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

CI = Path(__file__).resolve().parent.parent / "scripts" / "ci"


def run(script, *args, env=None):
    return subprocess.run(
        [sys.executable, str(CI / script), *map(str, args)],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


def junit(path, cases, suite_time=10.0):
    """A pytest-style junit file: <testsuites><testsuite> with (classname, name, seconds)."""
    rows = "".join(f'<testcase classname="{c}" name="{n}" time="{s}"/>' for c, n, s in cases)
    path.write_text(
        f'<?xml version="1.0"?><testsuites><testsuite name="pytest" time="{suite_time}" '
        f'tests="{len(cases)}">{rows}</testsuite></testsuites>'
    )
    return path


def start(tmp_path, seconds_ago):
    path = tmp_path / "step-start"
    path.write_text(f"{int(time.time()) - seconds_ago}\n")
    return path


def timing(tmp_path, *results, ago=30, go=False):
    summary = tmp_path / "summary.md"
    env = {
        "GITHUB_STEP_SUMMARY": str(summary),
        "GITHUB_RUN_ID": "4242",
        "HEAD_SHA": "0123456789abcdef",
        "GITHUB_EVENT_NAME": "pull_request",
    }
    args = ["--go"] if go else []
    args += ["--start", start(tmp_path, ago), "--out", tmp_path / "timing.json", *results]
    result = run("timing.py", *args, env=env)
    return result, summary


def test_summary_table_and_record_from_junit(tmp_path):
    cases = [("tests.test_a", f"test_{i}", i / 10) for i in range(25)]
    first = junit(tmp_path / "junit.xml", cases, suite_time=40.5)
    serial = junit(tmp_path / "junit-serial.xml", [("tests.test_b", "test_x[a|b]", 9.25)], 9.5)

    result, summary = timing(tmp_path, first, serial)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "::warning" not in result.stdout
    data = json.loads((tmp_path / "timing.json").read_text())
    assert data["run_id"] == "4242"
    assert data["sha"] == "0123456789abcdef"
    assert data["event"] == "pull_request"
    assert data["kind"] == "pytest"
    assert data["test_count"] == 26
    assert data["suite_seconds"] == 50.0
    assert 29 <= data["step_seconds"] <= 32
    assert len(data["slowest"]) == 20
    assert data["slowest"][0] == {"test": "tests.test_b::test_x[a|b]", "seconds": 9.25}
    assert data["slowest"][1] == {"test": "tests.test_a::test_24", "seconds": 2.4}
    text = summary.read_text()
    assert "| # | Test | Seconds |" in text
    assert "| 1 | `tests.test_b::test_x[a\\|b]` | 9.25 |" in text
    assert "| 20 | `tests.test_a::test_6` | 0.60 |" in text
    assert "test_5`" not in text
    assert "pytest: 50.0 s · 26 tests" in text


def test_a_test_over_60_seconds_warns(tmp_path):
    results = junit(tmp_path / "junit.xml", [("tests.t", "slow", 61.0), ("tests.t", "ok", 1)])

    result, _ = timing(tmp_path, results)

    assert result.returncode == 0
    assert "::warning title=Slow test::tests.t::slow took 61.0 s" in result.stdout
    assert "tests.t::ok" not in result.stdout


def test_a_test_of_90_seconds_fails_and_is_listed(tmp_path):
    cases = [("tests.t", "split_me", 90.0), ("tests.t", "also", 120.0), ("tests.t", "warn", 70)]
    results = junit(tmp_path / "junit.xml", cases)

    result, summary = timing(tmp_path, results)

    assert result.returncode == 1
    assert "::error title=CI time::tests.t::split_me took 90.0 s" in result.stdout
    assert "::error title=CI time::tests.t::also took 120.0 s" in result.stdout
    assert "::warning title=Slow test::tests.t::warn took 70.0 s" in result.stdout
    assert "CI time over budget:" in result.stdout
    # The summary and the record are there for the failed run too.
    assert "`tests.t::also`" in summary.read_text()
    assert json.loads((tmp_path / "timing.json").read_text())["slowest"][0]["seconds"] == 120.0


def test_a_step_over_90_seconds_warns(tmp_path):
    results = junit(tmp_path / "junit.xml", [("tests.t", "ok", 1)])

    result, _ = timing(tmp_path, results, ago=100)

    assert result.returncode == 0
    assert "::warning title=Slow test step::the test step took" in result.stdout


def test_a_step_over_240_seconds_fails(tmp_path):
    results = junit(tmp_path / "junit.xml", [("tests.t", "ok", 1)])

    result, _ = timing(tmp_path, results, ago=250)

    assert result.returncode == 1
    assert "over the 240 s limit" in result.stdout
    assert "::warning title=Slow test step" not in result.stdout


GO_PACKAGE = "github.com/example/proxy/api/internal/server"


def go_stream(path, events):
    lines = ["# github.com/example/proxy/api [build output that is not JSON]"]
    lines += [json.dumps(event) for event in events]
    path.write_text("\n".join(lines) + "\n")
    return path


def test_go_test_json_stream(tmp_path):
    pkg, other = GO_PACKAGE, "github.com/example/proxy/api/internal/jsnum"
    tests = go_stream(
        tmp_path / "go-tests.json",
        [
            {"Action": "start", "Package": pkg},
            {"Action": "run", "Package": pkg, "Test": "TestParts"},
            {"Action": "output", "Package": pkg, "Test": "TestParts", "Output": "=== RUN   x\n"},
            {
                "Action": "pass",
                "Package": pkg,
                "Test": "TestParts/QA-1/form/whole",
                "Elapsed": 38.5,
            },
            {"Action": "pass", "Package": pkg, "Test": "TestParts/QA-1", "Elapsed": 63.9},
            {"Action": "pass", "Package": pkg, "Test": "TestParts", "Elapsed": 64.1},
            {"Action": "output", "Package": pkg, "Output": "ok  \tserver\t64.7s\n"},
            {"Action": "pass", "Package": pkg, "Elapsed": 64.7},
        ],
    )
    numbers = go_stream(
        tmp_path / "go-latency.json",
        [
            {"Action": "skip", "Package": other, "Test": "TestNode", "Elapsed": 0},
            {"Action": "fail", "Package": other, "Test": "TestFixed", "Elapsed": 0.2},
            {"Action": "fail", "Package": other, "Elapsed": 0.3},
        ],
    )

    result, summary = timing(tmp_path, tests, numbers, go=True)

    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads((tmp_path / "timing.json").read_text())
    assert data["kind"] == "go"
    assert data["test_count"] == 5
    assert data["suite_seconds"] == 64.7
    assert [t["test"] for t in data["slowest"][:3]] == [
        "server TestParts",
        "server TestParts/QA-1",
        "server TestParts/QA-1/form/whole",
    ]
    assert data["slowest"][3] == {"test": "jsnum TestFixed", "seconds": 0.2}
    assert "::warning title=Slow test::server TestParts took 64.1 s" in result.stdout
    assert "::warning title=Slow test::server TestParts/QA-1 took 63.9 s" in result.stdout
    assert "slowest Go package: 64.7 s · 5 tests" in summary.read_text()

    log = run("timing.py", "--go", "--log", tests)
    assert log.returncode == 0
    assert log.stdout == "=== RUN   x\nok  \tserver\t64.7s\n"


def test_a_go_test_of_90_seconds_fails(tmp_path):
    stream = go_stream(
        tmp_path / "go-slow.json",
        [{"Action": "pass", "Package": GO_PACKAGE, "Test": "TestBig", "Elapsed": 182.0}],
    )

    result, _ = timing(tmp_path, stream, go=True)

    assert result.returncode == 1
    assert "server TestBig took 182.0 s (90 s or more: split it)" in result.stdout


def test_a_missing_junit_file_is_an_error_not_a_pass(tmp_path):
    present = junit(tmp_path / "junit.xml", [("tests.t", "ok", 1)])

    result, summary = timing(tmp_path, present, tmp_path / "junit-serial.xml")

    assert result.returncode == 1
    assert "::error title=CI time::" in result.stdout
    assert "junit-serial.xml: cannot read" in result.stdout
    assert not (tmp_path / "timing.json").exists()
    assert not summary.exists()


def test_results_without_a_test_or_unreadable_are_errors(tmp_path):
    empty = junit(tmp_path / "junit.xml", [])
    broken = tmp_path / "broken.xml"
    broken.write_text("<testsuites><testsuite")
    stream = go_stream(tmp_path / "go.json", [{"Action": "pass", "Package": GO_PACKAGE}])

    assert "no test in" in timing(tmp_path, empty)[0].stdout
    assert "not junit XML" in timing(tmp_path, broken)[0].stdout
    assert "no test in" in timing(tmp_path, stream, go=True)[0].stdout
    one = junit(tmp_path / "one.xml", [("tests.t", "ok", 1)])
    (tmp_path / "step-start").write_text("soon\n")
    bad_start = run(
        "timing.py", "--start", tmp_path / "step-start", "--out", tmp_path / "t.json", one
    )
    assert bad_start.returncode == 1
    assert "not a start time ('soon')" in bad_start.stdout
    assert run("timing.py", "--log", empty).returncode == 2
    assert run("timing.py", empty).returncode == 2


FAKE_GH = """\
import json, os, shutil, sys
from pathlib import Path
store = Path(os.environ["FAKE_GH_STORE"])
args = sys.argv[1:]
with open(store / "calls.txt", "a") as calls:
    calls.write(" ".join(args) + "\\n")
if args[:2] == ["run", "list"]:
    print((store / "runs.json").read_text())
elif args[:2] == ["run", "download"]:
    source = store / args[2] / args[args.index("--name") + 1]
    if not source.is_dir():
        sys.exit("no valid artifacts found to download")
    shutil.copytree(source, args[args.index("--dir") + 1])
"""


def fake_gh(tmp_path):
    store = tmp_path / "store"
    bin_dir = tmp_path / "bin"
    store.mkdir()
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(f"#!{sys.executable}\n{FAKE_GH}")
    gh.chmod(0o755)
    env = {"FAKE_GH_STORE": str(store), "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    return store, env


def artifact(store, run_id, name, step, slowest):
    folder = store / str(run_id) / name
    folder.mkdir(parents=True)
    data = {
        "run_id": str(run_id),
        "sha": f"{run_id}abcdef0123456",
        "event": "merge_group",
        "date": f"2026-11-0{run_id}T10:00:00Z",
        "kind": "pytest",
        "step_seconds": step,
        "suite_seconds": step - 10.0,
        "test_count": 900,
        "slowest": [{"test": t, "seconds": s} for t, s in slowest],
    }
    (folder / "timing.json").write_text(json.dumps(data))


def test_history_from_two_artifacts(tmp_path):
    store, env = fake_gh(tmp_path)
    runs = [
        {"databaseId": i, "createdAt": f"2026-11-0{i}T10:00:00Z", "headSha": "f"} for i in (3, 2, 1)
    ]
    (store / "runs.json").write_text(json.dumps(runs))
    artifact(store, 3, "ci-timing", 85, [("tests.a::slow", 40.0), ("tests.b::mid", 20.0)])
    artifact(store, 2, "ci-timing", 95, [("tests.a::slow", 45.5), ("tests.c::other", 30.0)])

    result = run("timing_history.py", "--runs", 5, "--event", "merge_group", env=env)

    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0].split() == [
        "date",
        "commit",
        "step",
        "s",
        "suite",
        "s",
        "tests",
        "slowest",
        "test",
    ]
    assert lines[1].split() == [
        "2026-11-03T10:00",
        "3abcdef012",
        "85",
        "75.0",
        "900",
        "40.0",
        "tests.a::slow",
    ]
    assert lines[2].split() == [
        "2026-11-02T10:00",
        "2abcdef012",
        "95",
        "85.0",
        "900",
        "45.5",
        "tests.a::slow",
    ]
    assert "2 runs with timing, 1 without" in result.stdout
    often = result.stdout.split("20 slowest (times, max seconds, test):\n")[1].splitlines()
    assert often[0].split() == ["2", "45.5", "tests.a::slow"]
    assert {line.split()[-1] for line in often[1:]} == {"tests.b::mid", "tests.c::other"}
    calls = (store / "calls.txt").read_text().splitlines()
    assert calls[0].startswith("run list --workflow ci.yml --status completed --limit 5 ")
    assert "--event merge_group" in calls[0]
    assert sum(call.startswith("run download") for call in calls) == 3


def test_history_of_the_api_workflow(tmp_path):
    store, env = fake_gh(tmp_path)
    (store / "runs.json").write_text(
        json.dumps([{"databaseId": 1, "createdAt": "", "headSha": ""}])
    )
    artifact(store, 1, "api-timing", 81, [])

    result = run(
        "timing_history.py", "--workflow", "api", "--repo", "o/r", "--branch", "main", env=env
    )

    assert result.returncode == 0, result.stderr
    assert "1 runs with timing, 0 without" in result.stdout
    assert "Most often" not in result.stdout
    calls = (store / "calls.txt").read_text()
    assert "--workflow api.yml" in calls and "--branch main" in calls and "-R o/r" in calls
    assert "--name api-timing" in calls


def test_history_fails_when_gh_fails(tmp_path):
    store, env = fake_gh(tmp_path)

    result = run("timing_history.py", env=env)

    assert result.returncode != 0
    assert "gh run list failed" in result.stderr


WORKFLOWS = CI.parent.parent / ".github" / "workflows"


def steps(workflow, job):
    return yaml.safe_load((WORKFLOWS / workflow).read_text())["jobs"][job]["steps"]


def timing_inputs(script, *, serial):
    """The results files the CI time step passes to timing.py, run with a stand-in python3."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "bin").mkdir()
        stub = root / "bin" / "python3"
        stub.write_text('#!/bin/sh\nfor a in "$@"; do echo "$a"; done\n')
        stub.chmod(0o755)
        (root / "junit.xml").touch()
        if serial:
            (root / "junit-serial.xml").touch()
        env = {"PATH": f"{root / 'bin'}:{os.environ['PATH']}", "EV": str(root)}
        out = subprocess.run(
            ["bash", "-c", script], env=env, capture_output=True, text=True, check=True
        ).stdout
        return [Path(line).name for line in out.splitlines() if line.endswith(".xml")]


def test_the_workflows_time_their_tests_and_keep_the_record():
    for workflow, job, name, artifact in (
        ("ci.yml", "tier1", "CI time (slowest tests, budgets)", "ci-timing"),
        ("api.yml", "api", "CI time (slowest Go tests, budgets)", "api-timing"),
    ):
        names = [step.get("name") for step in steps(workflow, job)]
        tests = next(s for s in steps(workflow, job) if s.get("id") == "tests")
        assert 'date +%s > "$EV/step-start"' in tests["run"], workflow
        timed = steps(workflow, job)[names.index(name)]
        # After the test steps, and also when they failed.
        assert names.index(name) > names.index(tests["name"]), workflow
        assert timed["if"].startswith("always() && "), workflow
        assert "steps.tests.outcome != 'skipped'" in timed["if"], workflow
        assert "scripts/ci/timing.py" in timed["run"] and "--start" in timed["run"], workflow
        upload = steps(workflow, job)[names.index(name) + 1]
        assert upload["uses"].startswith("actions/upload-artifact@"), workflow
        assert upload["if"] == "always() && steps.timing.outcome != 'skipped'", workflow
        assert upload["with"]["name"] == artifact, workflow
        assert upload["with"]["retention-days"] == 90, workflow
    ci = steps("ci.yml", "tier1")
    timed = next(s for s in ci if s.get("id") == "timing")
    # The serial results are read whenever the serial set ran (it runs only when a test is
    # marked serial, CI-FAST-2); the parallel results always.
    assert timing_inputs(timed["run"], serial=True) == ["junit.xml", "junit-serial.xml"]
    assert timing_inputs(timed["run"], serial=False) == ["junit.xml"]
    api = steps("api.yml", "api")
    assert (
        '--go --log "$EV/go-latency.json"'
        in next(s for s in api if "latency" in s.get("name", ""))["run"]
    )
    assert "--go --title" in next(s for s in api if s.get("id") == "timing")["run"]
