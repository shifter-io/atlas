"""CI-API-1: ci.yml's "What changed" step sets CODE and API, run as CI runs it."""

import os
import subprocess
from pathlib import Path

import pytest
from helpers import BASH, workflow_step


def git(repo: Path, *args: str) -> None:
    identity = ("-c", "user.name=ci", "-c", "user.email=ci@example.invalid")
    subprocess.run(["git", "-C", str(repo), *identity, *args], check=True, capture_output=True)


def changed(tmp_path: Path, files: list[str], base: str = "base") -> dict[str, str]:
    """The variables the step writes to $GITHUB_ENV for a head that adds `files`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    git(repo, "tag", "base")
    for name in files:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("changed\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", "head")
    github_env = tmp_path / "github-env"
    runner = tmp_path / "runner"
    runner.mkdir()
    env = {**os.environ, "BASE": base, "GITHUB_ENV": str(github_env), "RUNNER_TEMP": str(runner)}
    result = subprocess.run(
        [*BASH, workflow_step("What changed")], cwd=repo, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return dict(line.split("=", 1) for line in github_env.read_text().splitlines())


@pytest.mark.parametrize(
    ("files", "code", "api"),
    [
        # API-only: Go code and the API's own Markdown.
        (["api/internal/server/server.go", "api/README.md"], "false", "true"),
        # The framework's SQL, which the API's database tests load.
        (["src/proxy_index/calculated/schema.sql"], "true", "true"),
        (["src/proxy_index/schema.sql"], "true", "true"),
        # Docs-only, the API's Markdown outside api/fixtures/ included.
        (["docs/guide.md", "README.md", "api/FIELDS.md", "api/docs/notes.md"], "false", "false"),
        # Framework-only.
        (["src/proxy_index/cli.py", "tests/test_cli.py"], "true", "false"),
        # api/fixtures/ is test input, Markdown included.
        (["api/fixtures/measured/notes.md"], "false", "true"),
        ([".github/workflows/ci.yml"], "true", "true"),
    ],
)
def test_what_changed_sets_code_and_api(tmp_path, files, code, api):
    flags = changed(tmp_path, files)
    assert (flags["CODE"], flags["API"]) == (code, api)
    assert flags["BASE"] == "base"
    assert Path(flags["EV"]).is_dir()


def test_an_empty_diff_runs_everything(tmp_path):
    # The nightly run compares HEAD with itself.
    flags = changed(tmp_path, [], base="HEAD")
    assert (flags["CODE"], flags["API"]) == ("true", "true")
