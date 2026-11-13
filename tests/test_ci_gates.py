"""The two Tier-1 gate scripts in scripts/ci/, run as CI runs them (a subprocess per call)."""

import json
import subprocess
import sys
from pathlib import Path

CI = Path(__file__).resolve().parent.parent / "scripts" / "ci"


def run(script, *args):
    return subprocess.run(
        [sys.executable, str(CI / script), *map(str, args)], capture_output=True, text=True
    )


def git(repo, *args):
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=ci",
            "-c",
            "user.email=ci@example.invalid",
            *args,
        ],
        check=True,
        capture_output=True,
    )


def function(name, body_lines):
    return (
        f"def {name}():\n"
        + "".join(f"    x{i} = {i}\n" for i in range(body_lines - 1))
        + "    return 0\n"
    )


def repo_with(tmp_path, base, head):
    """A git repository whose base commit holds src/m.py = base and HEAD holds head."""
    git(tmp_path, "init", "-q")
    source = tmp_path / "src" / "m.py"
    source.parent.mkdir()
    source.write_text(base)
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    git(tmp_path, "tag", "base")
    source.write_text(head)
    git(tmp_path, "commit", "-qam", "head")
    return tmp_path


def test_limits_check_fails_a_new_81_line_function(tmp_path):
    repo = repo_with(tmp_path, function("small", 3), function("small", 3) + function("big", 80))
    result = run("limits_check.py", repo, "base", "HEAD")
    assert result.returncode == 1
    assert "HARD src/m.py:5 big: 81 lines (> 80; base new)" in result.stdout
    assert "RESULT FAIL (1 hard)" in result.stdout


def test_limits_check_allows_a_touched_legacy_function_that_does_not_grow(tmp_path):
    old = function("legacy", 90)
    repo = repo_with(tmp_path, old, old.replace("x5 = 5", "x5 = 6"))
    result = run("limits_check.py", repo, "base", "HEAD")
    assert result.returncode == 0
    assert "LEGACY src/m.py:1 legacy: 91 lines (> 80; base 91)" in result.stdout
    assert "RESULT PASS (0 hard)" in result.stdout


def moved_repo(tmp_path, base, head):
    """A repository whose base holds src/proxy_index/m.py = base, with the rename script and its
    keep-list, and whose HEAD moves it to src/atlas/m.py = head (RENAME-ATLAS-1)."""
    git(tmp_path, "init", "-q")
    (tmp_path / "scripts").mkdir()
    for name in ("rename_to_atlas.py", "rename_to_atlas.keep"):
        (tmp_path / "scripts" / name).write_bytes((CI.parent / name).read_bytes())
    old = tmp_path / "src" / "proxy_index" / "m.py"
    old.parent.mkdir(parents=True)
    old.write_text(base)
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    git(tmp_path, "tag", "base")
    git(tmp_path, "mv", "src/proxy_index", "src/atlas")
    (tmp_path / "src" / "atlas" / "m.py").write_text(head)
    git(tmp_path, "commit", "-qam", "head")
    return tmp_path


def test_limits_check_counts_only_what_the_rename_did_not_change(tmp_path):
    # Every line differs, so git sees no move; the rename pairs the two files and only NEW counts.
    base = "from proxy_index.storage import connect\nNAME = 'proxy-index'\n"
    head = "from atlas.storage import connect\nNAME = 'atlas'\nNEW = 1\n"
    result = run("limits_check.py", moved_repo(tmp_path, base, head), "base", "HEAD")
    assert result.returncode == 0
    assert "OK changed production lines: 1" in result.stdout


def test_limits_check_keeps_a_moved_legacy_function_legacy(tmp_path):
    old = function("legacy", 90).replace("x0 = 0", "x0 = 'proxy_index'")
    new = old.replace("'proxy_index'", "'atlas'").replace("x5 = 5", "x5 = 6")
    result = run("limits_check.py", moved_repo(tmp_path, old, new), "base", "HEAD")
    assert result.returncode == 0
    assert "OK changed production lines: 2" in result.stdout  # the x5 line, not the renamed one
    assert "LEGACY src/atlas/m.py:1 legacy: 91 lines (> 80; base 91)" in result.stdout


SOURCE = "def f(x):\n    if x:\n        return 1\n    return 0\n\n\ndef g():\n    return 2\n"


def coverage(tmp_path, record, branch=True):
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "m.py").write_text(SOURCE)
    data = {"meta": {"branch_coverage": branch}, "files": {"src/m.py": record}}
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(data))
    return path


def test_coverage_gate_fails_a_missing_branch_and_an_unknown_function(tmp_path):
    report = coverage(tmp_path, {"missing_lines": [], "missing_branches": [[2, 4]]})
    branch = run("coverage_gate.py", report, tmp_path, "src/m.py:f,g")
    assert branch.returncode == 1
    assert "FAIL src/m.py:f: missing lines [] missing branches from [2]" in branch.stdout
    assert "OK   src/m.py:g" in branch.stdout
    unknown = run("coverage_gate.py", report, tmp_path, "src/m.py:g,h")
    assert unknown.returncode == 1
    assert "FAIL src/m.py:h: function not found" in unknown.stdout


def test_coverage_gate_passes_complete_targets_and_needs_branch_data(tmp_path):
    report = coverage(tmp_path, {"missing_lines": [], "missing_branches": []})
    complete = run("coverage_gate.py", report, tmp_path, "src/m.py", "src/m.py:f,g")
    assert complete.returncode == 0
    assert "RESULT PASS (0 failing targets)" in complete.stdout
    no_branches = run(
        "coverage_gate.py",
        coverage(tmp_path, {"missing_lines": []}, branch=False),
        tmp_path,
        "src/m.py",
    )
    assert no_branches.returncode == 1
    assert "has no branch coverage" in no_branches.stdout
