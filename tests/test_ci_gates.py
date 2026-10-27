"""The three Tier-1 gate scripts in scripts/ci/, run as CI runs them (a subprocess per call);
the formatting-only attestor's AST normalisation is also tested directly."""

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

CI = Path(__file__).resolve().parent.parent / "scripts" / "ci"


def run(script, *args):
    return subprocess.run([sys.executable, str(CI / script), *map(str, args)],
                          capture_output=True, text=True)


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=ci", "-c",
                    "user.email=ci@example.invalid", *args], check=True, capture_output=True)


def function(name, body_lines):
    return f"def {name}():\n" + "".join(f"    x{i} = {i}\n" for i in range(body_lines - 1)) \
        + "    return 0\n"


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
    no_branches = run("coverage_gate.py", coverage(tmp_path, {"missing_lines": []}, branch=False),
                      tmp_path, "src/m.py")
    assert no_branches.returncode == 1
    assert "has no branch coverage" in no_branches.stdout


UGLY = "def f( a,b ):\n    x = {'a':1}\n    return a+b\n"


def ruff_format(repo, source, path="src/m.py"):
    command = [sys.executable, "-m", "ruff", "format", "--stdin-filename", path, "-"]
    return subprocess.run(command, input=source, cwd=repo, capture_output=True, text=True,
                          check=True).stdout


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def attested(repo, change, base=UGLY):
    """Commit pyproject.toml, src/m.py = base and docs/x.py = UGLY, let change(repo) edit the
    tree, commit it, and return the attestor's output for base..HEAD."""
    git(repo, "init", "-q")
    write(repo / "pyproject.toml", "[tool.ruff]\nline-length = 100\n")
    write(repo / "src" / "m.py", base)
    write(repo / "docs" / "x.py", UGLY)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    git(repo, "tag", "base")
    change(repo)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "head", "--allow-empty")
    result = run("format_only.py", repo, "base", "HEAD")
    assert result.returncode == 0
    return result.stdout


def formatted(repo, path="src/m.py", replace=("", "")):
    write(repo / path, ruff_format(repo, (repo / path).read_text(), path).replace(*replace))


def test_format_only_attests_the_exact_ruff_output_of_the_base(tmp_path):
    out = attested(tmp_path, formatted)
    base = subprocess.run(["git", "-C", tmp_path, "rev-parse", "base"], capture_output=True,
                          text=True, check=True).stdout.strip()
    ok, version, verdict = out.splitlines()
    assert (ok, verdict) == ("OK src/m.py", "FORMAT_ONLY=true")
    assert version.startswith("ruff ") and version.endswith(f", base {base}, 1 files")


def renamed(repo):
    formatted(repo)
    git(repo, "mv", "src/m.py", "src/k.py")


@pytest.mark.parametrize("change,reason", [
    (lambda repo: formatted(repo, replace=('"a": 1', '"a": 2')),
     "NOT src/m.py: not the ruff format output of the base"),
    (lambda repo: formatted(repo, replace=("\n    return", "\n\n    return")),
     "NOT src/m.py: not the ruff format output of the base"),
    (lambda repo: (formatted(repo), write(repo / "pyproject.toml", "[tool.ruff]\n")),
     "NOT pyproject.toml: not a .py file under src/, tests/ or scripts/"),
    (lambda repo: (formatted(repo), write(repo / "src" / "n.py", "x = 1\n")),
     "NOT src/n.py: status A"),
    (lambda repo: (repo / "src" / "m.py").unlink(), "NOT src/m.py: status D"),
    (renamed, "NOT src/k.py: status A"),
    (lambda repo: formatted(repo, "docs/x.py"),
     "NOT docs/x.py: not a .py file under src/, tests/ or scripts/"),
    (lambda repo: (formatted(repo), (repo / "src" / "m.py").chmod(0o755)),
     "NOT src/m.py: mode 100644 -> 100755"),
    (lambda repo: None, "NOT: no changed file\nruff "),
], ids=["literal", "blank-line", "pyproject", "added", "deleted", "renamed", "outside",
        "mode", "empty"])
def test_format_only_refuses_anything_but_the_ruff_output_of_modified_files(
        tmp_path, change, reason):
    out = attested(tmp_path, change)
    assert reason in out
    assert out.endswith("\nFORMAT_ONLY=false\n")


def test_format_only_attests_a_docstring_that_ruff_reindents(tmp_path):
    base = 'def f():\n    """   Doc.\n\n        More.   """\n    return 1\n'
    out = attested(tmp_path, formatted, base)
    after = (tmp_path / "src" / "m.py").read_text()
    docstrings = [ast.get_docstring(ast.parse(s).body[0], clean=False) for s in (base, after)]
    assert docstrings[0] != docstrings[1]
    assert out.startswith("OK src/m.py\n")
    assert out.endswith("\nFORMAT_ONLY=true\n")


def test_format_only_is_false_on_a_ruff_or_git_error(tmp_path):
    out = attested(tmp_path, lambda repo: write(repo / "src" / "m.py", "x = 1\n"), "def (:\n")
    assert "NOT src/m.py: ruff format failed: " in out
    assert out.endswith("\nFORMAT_ONLY=false\n")
    missing = run("format_only.py", tmp_path, "no-such-ref", "HEAD")
    assert missing.returncode == 0
    assert missing.stdout.startswith("error: ")
    assert missing.stdout.endswith("\nFORMAT_ONLY=false\n")


def test_format_only_needs_two_or_three_arguments():
    assert run("format_only.py", ".").returncode == 2
    assert run("format_only.py", ".", "a", "b", "c").returncode == 2


# Formats by turning every 1 into a 2: equal bytes with a different AST.
FAKE_RUFF = """import sys
if sys.argv[1] == "format":
    sys.stdout.write(sys.stdin.read().replace("1", "2"))
"""


@pytest.fixture
def attestor():
    spec = importlib.util.spec_from_file_location("format_only", CI / "format_only.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("before,after,same", [
    ("del (a, b)\n", "del a, b\n", True),
    ('x = u"x"\n', 'x = "x"\n', True),
    ('def f():\n    """  a\n      b  """\n', 'def f():\n    """a\n    b"""\n', True),
    ('x = "a b"\n', 'x = "a  b"\n', False),
    ("x = 1\n", "x = 2\n", False),
])
def test_format_only_compares_asts_after_blacks_normalisations(attestor, before, after, same):
    assert (attestor.normal_ast(before) == attestor.normal_ast(after)) is same


def test_format_only_refuses_equal_bytes_with_a_different_ast(tmp_path, attestor, monkeypatch,
                                                              capsys):
    """Rule 4 holds on its own: a formatter that changed a value is caught by the AST check."""
    monkeypatch.setattr(attestor, "RUFF", [sys.executable, "-c", FAKE_RUFF])
    attested(tmp_path, lambda repo: write(repo / "src" / "m.py", "x = 2\n"), "x = 1\n")
    assert attestor.attest(str(tmp_path), "base", "HEAD") is False
    assert "NOT src/m.py: the AST differs" in capsys.readouterr().out
