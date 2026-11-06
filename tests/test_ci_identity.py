"""REPO-MERGE-1b: the commit-identity check and the gitleaks step, run as CI runs them."""

from __future__ import annotations

import os
import secrets
import string
import subprocess
import sys
from pathlib import Path

import pytest
from helpers import BASH, CI_WORKFLOW, workflow_step

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "scripts" / "ci" / "identity_check.py"
# Joined at run time so this file adds no literal address the privacy step would flag.
DOMAIN = ".".join(("users", "noreply", "github", "com"))
OWNER = f"16010431+ice-zeus@{DOMAIN}"
WEB_FLOW = "noreply@" + ".".join(("github", "com"))
PERSONAL = "dev@example.invalid"
AI = "noreply@" + ".".join(("anthropic", "com"))


def git(repo: Path, *args: str, author: str = OWNER, committer: str = OWNER) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "a",
        "GIT_AUTHOR_EMAIL": author,
        "GIT_COMMITTER_NAME": "c",
        "GIT_COMMITTER_EMAIL": committer,
    }
    done = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env
    )
    return done.stdout.strip()


def repo_at_base(tmp_path: Path) -> Path:
    tmp_path.mkdir(exist_ok=True)
    git(tmp_path, "init", "-q")
    git(tmp_path, "commit", "-q", "--allow-empty", "-m", "base", author=PERSONAL)
    git(tmp_path, "tag", "base")
    return tmp_path


def commit(repo: Path, message: str, **who: str) -> str:
    git(repo, "commit", "-q", "--allow-empty", "-m", message, **who)
    return git(repo, "rev-parse", "HEAD")


def check(repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECK), str(repo), "base", "HEAD"], capture_output=True, text=True
    )


def test_noreply_identities_and_the_web_flow_committer_pass(tmp_path):
    repo = repo_at_base(tmp_path)
    commit(repo, f"one\n\nCo-authored-by: Helper <42+helper@{DOMAIN}>")
    commit(repo, "squash merged on GitHub", committer=WEB_FLOW)
    result = check(repo)
    assert result.returncode == 0, result.stdout
    assert "identity: 2 commits checked, 0 violations" in result.stdout


def test_a_personal_author_fails_without_printing_the_address(tmp_path):
    repo = repo_at_base(tmp_path)
    commit(repo, "fine")
    bad = commit(repo, "personal worktree", author=PERSONAL)
    result = check(repo)
    assert result.returncode == 1
    assert f"{bad[:12]} author email is not a GitHub noreply address" in result.stdout
    assert "1 violations" in result.stdout
    assert PERSONAL not in result.stdout + result.stderr


def test_a_personal_committer_and_a_web_flow_author_fail(tmp_path):
    repo = repo_at_base(tmp_path)
    committed = commit(repo, "x", committer=PERSONAL)
    authored = commit(repo, "y", author=WEB_FLOW)
    result = check(repo)
    assert result.returncode == 1
    assert f"{committed[:12]} committer email is not" in result.stdout
    assert f"{authored[:12]} author email is not" in result.stdout


@pytest.mark.parametrize(
    "trailer",
    [
        f"Co-authored-by: Dev <{PERSONAL}>",
        f"Co-Authored-By: Claude <{AI}>",
        "co-authored-by: nobody",
        f"Co-authored-by: Two <1+a@{DOMAIN}> <{PERSONAL}>",
    ],
)
def test_a_co_author_without_a_noreply_address_fails(tmp_path, trailer):
    repo = repo_at_base(tmp_path)
    commit(repo, f"one\n\nCo-authored-by: Fine <1+a@{DOMAIN}>\n{trailer}")
    result = check(repo)
    assert result.returncode == 1
    assert "Co-authored-by line 2 has no GitHub noreply address" in result.stdout
    assert "line 1" not in result.stdout


def test_a_co_author_line_outside_the_trailer_block_counts(tmp_path):
    repo = repo_at_base(tmp_path)
    commit(repo, f"one\n\nCo-authored-by: Dev <{PERSONAL}>\n\nlast paragraph")
    assert check(repo).returncode == 1


def test_bad_usage_exits_2():
    assert subprocess.run([sys.executable, str(CHECK)], capture_output=True).returncode == 2


def test_both_guards_run_for_every_change_before_any_build_step():
    text = CI_WORKFLOW.read_text()
    steps = text.split("\n      - name: ")
    names = [step.split("\n", 1)[0] for step in steps[1:]]
    for name in ("Commit identities", "No secrets in the commits", "Pinned gitleaks"):
        index = next(i for i, n in enumerate(names) if n.startswith(name))
        assert index < names.index("uv sync (locked)")
        assert "if:" not in steps[index + 1].split("run:", 1)[0], name
    identity = next(s for s in steps if s.startswith("Commit identities"))
    assert 'identity_check.py . "$BASE" HEAD' in identity


def _secret_scan(repo: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    ev = tmp_path / "ev"
    ev.mkdir()
    env = {**os.environ, "BASE": "base", "EV": str(ev)}
    return subprocess.run(
        [*BASH, workflow_step("No secrets in the commits")],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )


needs_gitleaks = pytest.mark.skipif(
    not os.environ.get("GITLEAKS"), reason="Set GITLEAKS to a gitleaks binary (CI sets it)"
)


@needs_gitleaks
def test_the_secret_step_fails_a_seeded_token_and_prints_no_match(tmp_path):
    repo = repo_at_base(tmp_path / "repo")
    # Built at run time, so the repository never holds a secret-shaped literal.
    alphabet = string.ascii_letters + string.digits
    token = "ghp_" + "".join(secrets.choice(alphabet) for _ in range(36))
    (repo / "settings.toml").write_text(f'github_token = "{token}"\n')
    git(repo, "add", "settings.toml")
    leaked = commit(repo, "add settings")
    result = _secret_scan(repo, tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert f"in settings.toml at {leaked[:12]}" in result.stdout
    assert token not in result.stdout + result.stderr


@needs_gitleaks
def test_the_secret_step_passes_a_clean_range(tmp_path):
    repo = repo_at_base(tmp_path / "repo")
    (repo / "notes.txt").write_text("nothing secret here\n")
    git(repo, "add", "notes.txt")
    commit(repo, "add notes")
    result = _secret_scan(repo, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
