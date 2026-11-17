"""PUBLIC-CI-1: a fork's or a bot's pull request gets no CI server, and its required checks fail.

The workflows' own guard, read as GitHub reads it. The security boundary is outside the repository
(the servers' job-started hook, the runner group, GitHub's approval of outside contributors' runs);
these tests keep the honest case right: no self-hosted job queued for a fork, never a skipped check.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
# A pull request may use the CI server only from a branch of this repository, opened by a person.
OWN = (
    "github.event_name != 'pull_request' || (github.event.pull_request.head.repo.full_name"
    " == github.repository && github.event.pull_request.user.type == 'User')"
)
GUARD = "Maintainers' pull requests only (a fork never reaches the CI server)"
# Triggers that run with the base repository's rights on outside input.
FORBIDDEN = {"pull_request_target", "workflow_run", "issue_comment"}
RUNNER = f'({OWN}) && \'["self-hosted", "ci-32cpu"]\' || \'"ubuntu-latest"\''
NIGHTLY = "github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'"
SHA = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
# The only events a workflow with a CI-server job may have: the maintainers' own (a pull request
# behind the guard above, the queue, a schedule or push on the default branch, a dispatch). Every
# other event an outsider can start (an issue, a comment, a review, a fork, a release, a label).
SELF_HOSTED_TRIGGERS = {"pull_request", "merge_group", "schedule", "workflow_dispatch", "push"}


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _flows() -> list[tuple[str, dict]]:
    return [(p.name, _load(p)) for p in sorted(WORKFLOWS.glob("*.yml"))]


def _triggers(flow: dict) -> set[str]:
    # PyYAML reads the `on:` key as True.
    on = flow[True]
    return {on} if isinstance(on, str) else set(on)


def _self_hosted(job: dict) -> bool:
    return "self-hosted" in str(job.get("runs-on"))


def _outside_triggers(flow: dict) -> set[str]:
    """The triggers outside SELF_HOSTED_TRIGGERS of a workflow with a CI-server job, else none."""
    if not any(_self_hosted(job) for job in flow["jobs"].values()):
        return set()
    return _triggers(flow) - SELF_HOSTED_TRIGGERS


def _evaluate(expression: str, context: dict) -> bool:
    """A GitHub expression of `==`, `!=`, `&&`, `||`, `!` and parentheses over context paths."""
    python = expression.replace("&&", " and ").replace("||", " or ")
    python = re.sub(r"!(?!=)", " not ", python)
    path = re.compile(r"\bgithub(\.[A-Za-z_]+)+")
    python = path.sub(lambda m: repr(_lookup(context, m.group(0))), python)
    # Our own fixed expression, with every context value already a Python literal.
    return bool(eval(python, {"__builtins__": {}}))


def _lookup(context: dict, path: str):
    value = context
    for key in path.split(".")[1:]:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _pull_request(head: str | None, kind: str = "User") -> dict:
    repo = {"full_name": head} if head else None
    pr = {"head": {"repo": repo}, "user": {"type": kind}}
    return {"event_name": "pull_request", "repository": "o/atlas", "event": {"pull_request": pr}}


@pytest.mark.parametrize(
    ("context", "own"),
    [
        (_pull_request("o/atlas"), True),
        (_pull_request("outsider/atlas"), False),
        (_pull_request("o/atlas", kind="Bot"), False),
        # A fork deleted after its pull request opened has no head repository.
        (_pull_request(None), False),
        ({"event_name": "merge_group", "repository": "o/atlas"}, True),
        ({"event_name": "workflow_dispatch", "repository": "o/atlas"}, True),
    ],
)
def test_only_this_repositorys_branches_by_a_person_count_as_the_maintainers(context, own):
    assert _evaluate(OWN, context) is own


def test_no_workflow_runs_on_outside_input_with_the_base_repositorys_rights():
    for name, flow in _flows():
        assert not _triggers(flow) & FORBIDDEN, name


def test_every_self_hosted_job_of_a_pull_request_workflow_is_guarded_and_fails_closed():
    guarded = []
    for name, flow in _flows():
        for job_id, job in flow["jobs"].items():
            if not _self_hosted(job) or "pull_request" not in _triggers(flow):
                continue
            guarded.append(f"{name}:{job_id}")
            # GitHub's runner for anyone else, the CI server only when OWN holds.
            assert RUNNER in job["runs-on"]
            guard = next(s for s in job["steps"] if s.get("name") == GUARD)
            assert job["steps"].index(guard) <= 1, f"{name}:{job_id}"
            assert guard["if"].startswith("${{ ") and f"!({OWN}) }}}}" in guard["if"]
            # Never skipped: it fails the check.
            assert subprocess.run(["bash", "-c", guard["run"]], capture_output=True).returncode == 1
            assert "if" not in job or "pull_request.head" not in job["if"], "a skipped check passes"
    assert guarded == ["api.yml:api", "ci.yml:tier1"]


def test_the_nightly_ci_server_job_runs_only_on_its_schedule_and_dispatches():
    flow = _load(WORKFLOWS / "maestro-nightly.yml")
    assert _triggers(flow) == {"schedule", "workflow_dispatch"}
    audit = flow["jobs"]["audit"]
    assert audit["if"] == NIGHTLY


def test_every_action_is_pinned_to_a_full_commit():
    for name, flow in _flows():
        for job in flow["jobs"].values():
            for step in job.get("steps", []):
                if "uses" in step:
                    assert SHA.match(step["uses"]), f"{name}: {step['uses']}"


def test_a_workflow_with_a_ci_server_job_has_only_the_maintainers_triggers():
    servers = []
    for name, flow in _flows():
        if any(_self_hosted(job) for job in flow["jobs"].values()):
            servers.append(name)
        assert not _outside_triggers(flow), f"{name}: {sorted(_outside_triggers(flow))}"
    assert servers == ["api.yml", "ci.yml", "maestro-nightly.yml"]


@pytest.mark.parametrize(
    "event", ["issues", "pull_request_review", "pull_request_review_comment", "fork", "release"]
)
def test_an_event_an_outsider_starts_is_caught_on_a_ci_server_workflow(event):
    server = {"runs-on": ["self-hosted", "ci-32cpu"], "steps": []}
    github = {"runs-on": "ubuntu-latest", "steps": []}
    on = {"workflow_dispatch": None, event: None}
    assert _outside_triggers({True: on, "jobs": {"job": server}}) == {event}
    assert _outside_triggers({True: event, "jobs": {"job": server}}) == {event}
    # GitHub's runner may run on any event: only the CI servers are restricted.
    assert _outside_triggers({True: on, "jobs": {"job": github}}) == set()
    allowed = {trigger: None for trigger in SELF_HOSTED_TRIGGERS}
    assert _outside_triggers({True: allowed, "jobs": {"job": server}}) == set()
