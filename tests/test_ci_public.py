"""PUBLIC-CI-1: public.yml, the tests on GitHub's runners for every pull request, a fork's too."""

from __future__ import annotations

import hashlib
import importlib.util
import subprocess
import tarfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
FETCH = ROOT / "scripts" / "ci" / "fetch_tool.sh"
_spec = importlib.util.spec_from_file_location("shard", ROOT / "scripts" / "ci" / "shard.py")
shard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shard)


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())


PUBLIC = _load("public.yml")


def test_it_runs_for_every_pull_request_on_githubs_runners_only():
    on = PUBLIC[True]  # PyYAML reads the `on:` key as True.
    assert set(on) == {"pull_request", "schedule", "workflow_dispatch"}
    assert "paths" not in on["pull_request"]
    assert PUBLIC["permissions"] == {"contents": "read"}
    for job in PUBLIC["jobs"].values():
        assert job["runs-on"] == "ubuntu-latest"
        for step in job["steps"]:
            assert "secrets." not in str(step)


def test_its_pins_are_tier1s_and_the_apis():
    env = PUBLIC["env"]
    for name, value in {**_load("ci.yml")["env"], **_load("api.yml")["env"]}.items():
        if name in env:
            assert env[name] == value, name
    assert {"UV_SHA256", "PG_SHA256", "GITLEAKS_SHA256", "GO_SHA256"} <= set(env)


def test_it_skips_exactly_the_apis_performance_tests():
    api = _load("api.yml")["env"]
    names = set()
    for key in ("PEAK_RSS_TEST", "LATENCY_TEST", "PAGE_LATENCY_TEST"):
        names |= set(api[key].strip("^$()").split("|"))
    assert set(PUBLIC["env"]["GO_PERF_TESTS"].strip("^$()").split("|")) == names


def test_public_passes_only_when_every_job_passed():
    job = PUBLIC["jobs"]["public"]
    assert set(job["needs"]) == {"checks", "python", "go"}
    assert job["if"].startswith("${{ !cancelled() && ")
    run = job["steps"][0]["run"]

    def status(results: str) -> int:
        return subprocess.run(["bash", "-c", run], env={"RESULTS": results}).returncode

    assert status("success success success") == 0
    for bad in ("success failure success", "success skipped success", "cancelled success success"):
        assert status(bad) == 1, bad


def test_the_matrix_and_the_shard_script_agree():
    matrix = PUBLIC["jobs"]["python"]["strategy"]["matrix"]["shard"]
    steps = PUBLIC["jobs"]["python"]["steps"]
    run = next(s["run"] for s in steps if "shard.py" in s.get("run", ""))
    assert matrix == list(range(1, len(matrix) + 1))
    assert f'scripts/ci/shard.py "$SHARD" {len(matrix)}' in run


def test_every_test_file_is_in_exactly_one_balanced_shard():
    weights = shard.weights()
    assert len(weights) > 100
    parts = shard.shards(weights, 6)
    names = [name for part in parts for name in part]
    assert sorted(names) == sorted(weights)
    loads = [sum(weights[name] for name in part) for part in parts]
    assert max(loads) <= sum(loads) / 6 + max(weights.values())
    assert shard.shards(weights, 6) == parts


def test_a_file_without_recorded_durations_still_weighs_something(tmp_path):
    (tmp_path / "tests").mkdir()
    for name in ("test_a.py", "test_b.py", "helpers.py"):
        (tmp_path / "tests" / name).write_text("")
    (tmp_path / "tests" / "durations.json").write_text('{"tests/test_a.py::test_x": 5}')
    assert shard.weights(tmp_path) == {"tests/test_a.py": 5.0, "tests/test_b.py": shard.DEFAULT}
    assert shard.shards(shard.weights(tmp_path), 2) == [["tests/test_a.py"], ["tests/test_b.py"]]


def test_bad_shard_arguments_exit_2(capsys):
    for args in ([], ["0", "6"], ["7", "6"], ["x", "6"]):
        assert shard.main(["shard.py", *args]) == 2
    assert shard.main(["shard.py", "1", "1"]) == 0
    assert "tests/test_ci_public.py" in capsys.readouterr().out


def _archive(tmp_path: Path) -> tuple[str, str]:
    (tmp_path / "src" / "tool-1").mkdir(parents=True)
    (tmp_path / "src" / "tool-1" / "bin").write_text("x")
    archive = tmp_path / "tool.tgz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(tmp_path / "src" / "tool-1", arcname="tool-1")
    return archive.as_uri(), hashlib.sha256(archive.read_bytes()).hexdigest()


def test_the_fetch_unpacks_a_checked_archive_and_refuses_a_wrong_one(tmp_path):
    url, digest = _archive(tmp_path)
    good, bad = tmp_path / "tools" / "good", tmp_path / "tools" / "bad"
    assert subprocess.run([str(FETCH), url, digest, str(good)]).returncode == 0
    assert (good / "bin").read_text() == "x" and (good / ".complete").exists()
    assert subprocess.run([str(FETCH), url, "0" * 64, str(bad)], capture_output=True).returncode
    assert not bad.exists()
    assert list((tmp_path / "tools").iterdir()) == [good]
    assert subprocess.run([str(FETCH), url], capture_output=True).returncode == 2
