"""scripts/ci/durations.py: tests/durations.json from tier1's junit, the order conftest.py reads."""

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import conftest
import pytest
from test_ci_timing import fake_gh, junit, steps

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci" / "durations.py"
# The fresh pytest of test_node_ids_are_pytest_s_own must not join this run's xdist,
# coverage or timeline.
INHERITED = ("PYTEST_XDIST", "CI_TIMELINE", "COV_CORE", "COVERAGE")


@pytest.fixture
def script(monkeypatch):
    # durations.py imports timing.py from its own folder, as `python3 scripts/ci/...` does.
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    return runpy.run_path(str(SCRIPT))


def run(*args, env=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, args)],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )


def tree(root, *files):
    for name in files:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("")
    return root


def test_node_id_is_the_longest_classname_prefix_that_is_a_file(tmp_path, script):
    root = tree(tmp_path, "tests/test_a.py", "tests/sub/test_b.py", "tests/test_a/test_c.py")
    node_id = script["node_id"]
    assert node_id("tests.test_a::test_x", root) == "tests/test_a.py::test_x"
    assert node_id("tests.test_a.TestC.TestD::test_y[1.5-a::b]", root) == (
        "tests/test_a.py::TestC::TestD::test_y[1.5-a::b]"
    )
    assert node_id("tests.sub.test_b::test_z", root) == "tests/sub/test_b.py::test_z"
    assert node_id("tests.test_a.test_c::test_w", root) == "tests/test_a/test_c.py::test_w"
    # A test whose file is gone, and a collection error (no classname).
    assert node_id("tests.test_gone::test_x", root) is None
    assert node_id("::tests.test_a", root) is None


def test_node_ids_are_pytest_s_own(tmp_path, script):
    """A real pytest's junit maps back to exactly the node ids it collects."""
    tree(tmp_path, "pytest.ini")
    (tmp_path / "tests" / "sub").mkdir(parents=True)
    (tmp_path / "tests" / "sub" / "test_real.py").write_text(
        "import pytest\n\n"
        "def test_plain():\n    pass\n\n"
        "class TestOuter:\n"
        "    class TestInner:\n"
        "        @pytest.mark.parametrize('value', [1.5, 'a/b.c'])\n"
        "        def test_param(self, value):\n            pass\n"
    )
    environment = {k: v for k, v in os.environ.items() if not k.startswith(INHERITED)}

    def pytest_run(*args):
        command = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-p", "no:xdist"]
        return subprocess.run(
            [*command, *args, "tests"],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    collected = [line for line in pytest_run("--collect-only", "-q").splitlines() if "::" in line]
    pytest_run("-q", "--junitxml", str(tmp_path / "junit.xml"))
    found = script["durations"]([tmp_path / "junit.xml"], 0.0, tmp_path)
    assert len(collected) == 3
    assert sorted(found) == sorted(collected)


def test_durations_average_the_runs_and_keep_the_slow_tests(tmp_path, script):
    root = tree(tmp_path / "repo", "tests/test_a.py", "tests/test_b.py")
    first = junit(
        tmp_path / "first.xml",
        [
            ("tests.test_a", "test_slow", 2.0),
            ("tests.test_a.TestC", "test_fast", 0.4),
            ("tests.test_b", "test_once", 1.04),
            ("tests.test_gone", "test_moved", 50.0),
        ],
    )
    second = junit(tmp_path / "second.xml", [("tests.test_a", "test_slow", 3.0)])
    assert script["durations"]([first, second], 1.0, root) == {
        "tests/test_a.py::test_slow": 2.5,
        "tests/test_b.py::test_once": 1.0,
    }
    assert script["durations"]([first], 0.0, root)["tests/test_a.py::TestC::test_fast"] == 0.4


def test_results_are_the_files_and_each_folder_s_junit(tmp_path, script):
    folder = tmp_path / "artifact"
    folder.mkdir()
    for name in ("junit.xml", "junit-serial.xml", "timing.json"):
        (folder / name).write_text("")
    one = tmp_path / "one.xml"
    assert script["results"]([one, folder]) == [
        one,
        folder / "junit-serial.xml",
        folder / "junit.xml",
    ]
    (tmp_path / "empty").mkdir()
    with pytest.raises(script["TimingError"], match="empty: no junit"):
        script["results"]([one, tmp_path / "empty"])


def this_file_junit(folder, name, cases):
    """A junit file in `folder` whose tests are tests of this file (so their file exists)."""
    folder.mkdir(parents=True, exist_ok=True)
    rows = [("tests.test_ci_durations", test, seconds) for test, seconds in cases]
    return junit(folder / name, rows)


def test_the_cli_writes_the_file_conftest_reads(tmp_path):
    folder = tmp_path / "ci-timing"
    this_file_junit(folder, "junit.xml", [("test_b", 3.04), ("test_a", 1.5), ("test_c", 0.2)])
    this_file_junit(folder, "junit-serial.xml", [("test_d", 1.0)])
    out = tmp_path / "durations.json"

    result = run(folder, "--out", out)

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"3 tests of at least 1 s written to {out}\n"
    test = "tests/test_ci_durations.py::"
    expected = {f"{test}test_a": 1.5, f"{test}test_b": 3.0, f"{test}test_d": 1.0}
    assert out.read_text() == json.dumps(expected, indent=0, sort_keys=True) + "\n"

    class Item:
        def __init__(self, nodeid):
            self.nodeid = nodeid

    items = [Item(f"{test}test_{name}") for name in "abcd"]
    ordered = conftest.spread(items, json.loads(out.read_text()), 2)
    assert [item.nodeid[len(test) :] for item in ordered] == [
        "test_b",
        "test_c",
        "test_a",
        "test_d",
    ]


def test_from_run_downloads_the_run_s_ci_timing_artifact(tmp_path):
    store, env = fake_gh(tmp_path)
    this_file_junit(store / "7" / "ci-timing", "junit.xml", [("test_x", 4.0)])
    out = tmp_path / "durations.json"

    result = run("--from-run", 7, "--repo", "owner/repo", "--out", out, "--min", 2, env=env)

    assert result.returncode == 0, result.stderr
    assert json.loads(out.read_text()) == {"tests/test_ci_durations.py::test_x": 4.0}
    call = (store / "calls.txt").read_text().split()
    assert call[:5] == ["run", "download", "7", "--name", "ci-timing"]
    assert call[-2:] == ["-R", "owner/repo"]


def test_errors_write_nothing(tmp_path):
    store, env = fake_gh(tmp_path)
    out = tmp_path / "durations.json"
    (tmp_path / "empty").mkdir()
    (tmp_path / "bad.xml").write_text("not xml")

    missing = run("--from-run", 8, "--out", out, env=env)
    empty = run(tmp_path / "empty", "--out", out)
    bad = run(tmp_path / "bad.xml", "--out", out)

    assert missing.returncode == 1 and "run 8: no ci-timing artifact" in missing.stderr
    assert empty.returncode == 1 and "no junit*.xml" in empty.stderr
    assert bad.returncode == 1 and "not junit XML" in bad.stderr
    assert not out.exists()
    assert run().returncode == 2


def test_the_committed_durations_are_in_conftest_s_format():
    durations = json.loads(conftest.DURATIONS.read_text())
    assert durations, "tests/durations.json is empty"
    for nodeid, seconds in durations.items():
        assert nodeid.startswith("tests/") and ".py::" in nodeid, nodeid
        assert isinstance(seconds, (int, float)) and seconds >= 1.0, nodeid


def test_tier1_keeps_the_junit_and_reports_each_worker_s_end():
    tier1 = steps("ci.yml", "tier1")
    tests = next(s for s in tier1 if s.get("id") == "tests")
    timed = next(s for s in tier1 if s.get("id") == "timing")
    upload = tier1[tier1.index(timed) + 1]
    assert "CI_TIMELINE=1 uv run --no-editable pytest" in tests["run"]
    assert 'scripts/ci/timeline.py "$EV"' in timed["run"]
    assert "${{ runner.temp }}/ev/junit*.xml" in upload["with"]["path"].splitlines()
