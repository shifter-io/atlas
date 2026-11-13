"""api.yml reports on every pull request (CI-GATE-1): its changed-files step, run as CI runs it."""

import os
import subprocess
from pathlib import Path

import yaml
from test_ci_gates import git

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "api.yml"


def workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def decide(tmp_path, changed, event="pull_request"):
    """The `code` output of api-changes' diff step for a head that changes `changed`."""
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    (repo / "README.md").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    git(repo, "tag", "base")
    for path in changed:
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text("head\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "head")
    step = next(s for s in workflow()["jobs"]["api-changes"]["steps"] if s.get("id") == "diff")
    output = tmp_path / "output"
    env = {**os.environ, "BASE": "base", "GITHUB_OUTPUT": str(output), "GITHUB_EVENT_NAME": event}
    subprocess.run(["bash", "-eo", "pipefail", "-c", step["run"]], cwd=repo, env=env, check=True)
    return output.read_text().strip()


def test_untouched_api_decides_no_code(tmp_path):
    untouched = ["src/atlas/runner.py", "docs/x.md", "api/README.md"]
    assert decide(tmp_path, untouched) == "code=false"


def test_api_code_sql_fixtures_and_the_workflow_decide_code(tmp_path):
    for i, path in enumerate(
        [
            "api/internal/server/x.go",
            "src/atlas/calculated/schema.sql",
            "api/fixtures/answer.md",
            ".github/workflows/api.yml",
        ]
    ):
        assert decide(tmp_path / str(i), [path]) == "code=true", path


def test_nightly_and_dispatches_always_test(tmp_path):
    assert decide(tmp_path / "s", ["docs/x.md"], event="schedule") == "code=true"
    assert decide(tmp_path / "d", ["docs/x.md"], event="workflow_dispatch") == "code=true"


def test_api_reports_on_every_pull_request_and_fails_when_the_decision_failed():
    flow = workflow()
    # PyYAML reads the `on:` key as True.
    assert "paths" not in flow[True]["pull_request"]
    changes, api = flow["jobs"]["api-changes"], flow["jobs"]["api"]
    assert changes["runs-on"] == "ubuntu-latest"
    assert api["needs"] == "api-changes"
    # Never skipped because api-changes failed: a skipped required check counts as passed.
    assert api["if"].startswith("${{ !cancelled() && ")
    first = api["steps"][0]
    assert first["env"]["RESULT"] == "${{ needs.api-changes.result }}"
    result = subprocess.run(["bash", "-c", first["run"]], env={"RESULT": "failure"})
    assert result.returncode == 1
    assert subprocess.run(["bash", "-c", first["run"]], env={"RESULT": "success"}).returncode == 0
    # The CI server, the checkout, Go, PostgreSQL and the tests only when the API changed.
    assert """'["self-hosted", "ci-32cpu"]'""" in api["runs-on"]
    assert """'"ubuntu-latest"'""" in api["runs-on"]
    code = "needs.api-changes.outputs.code == 'true'"
    gated = [s for s in api["steps"] if code in s.get("if", "")]
    assert gated[0]["uses"].startswith("actions/checkout@")
    assert {"PostgreSQL 17", "Pinned Go in the server cache"} <= {s.get("name") for s in gated}
    assert sum("go test" in s.get("run", "") for s in gated) >= 3
