"""CI-90-6: a test's teardown queues its scratch databases' drops for a dropper process."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import conftest
import dropper
import helpers
import psycopg
import pytest
from test_storage import admin_credentials


class Process:
    """A fake dropper process: keeps what it is sent, exits with `code`."""

    def __init__(self, code=0, errors=""):
        self.stdin, self.stderr, self.code = io.StringIO(), io.StringIO(errors), code
        self.stdin.close = lambda: setattr(self, "sent", self.stdin.getvalue())

    def wait(self):
        self.returncode = self.code
        return self.code


@pytest.fixture
def spawned(monkeypatch):
    """The fake dropper processes queue starts, with the arguments each one was given."""
    started = []

    def popen(args, **kwargs):
        started.append((args, kwargs, Process(*outcome)))
        return started[-1][2]

    outcome = []
    monkeypatch.setattr(dropper, "PROCESS", None)
    monkeypatch.setattr(dropper, "_POPEN", popen)
    return started, outcome


def test_a_drop_is_sent_to_one_dropper_and_the_session_end_waits(spawned):
    started, _ = spawned
    helpers.drop_databases({"user": "a"}, ["one", "two"])
    helpers.drop_databases({"user": "b"}, ("three",))
    ((args, kwargs, process),) = started
    assert args[1:] == [str(dropper.Path(dropper.__file__).resolve())]
    assert kwargs["stdin"] is dropper.subprocess.PIPE
    helpers.drop_queued()
    assert [json.loads(line) for line in process.sent.splitlines()] == [
        {"admin": {"user": "a"}, "names": ["one", "two"]},
        {"admin": {"user": "b"}, "names": ["three"]},
    ]
    assert dropper.PROCESS is None
    # Nothing queued: nothing to wait for; a later drop starts a new dropper.
    helpers.drop_queued()
    helpers.drop_databases({"user": "a"}, ["four"])
    assert len(started) == 2


def test_a_failed_dropper_fails_the_session_end(spawned):
    started, outcome = spawned
    outcome[:] = [1, "dropping ['lost']: refused\n"]
    helpers.drop_databases({"user": "a"}, ["lost"])
    with pytest.raises(dropper.DropError, match=r"exit 1\): dropping \['lost'\]: refused"):
        helpers.drop_queued()
    assert dropper.PROCESS is None


def test_the_dropper_drops_each_line_and_reports_every_failure(capsys):
    dropped = []

    def drop(admin, names):
        if admin["user"] == "broken":
            raise psycopg.OperationalError("refused")
        dropped.append((admin["user"], names))

    lines = [
        json.dumps({"admin": {"user": user, "password": "secret"}, "names": names}) + "\n"
        for user, names in (("broken", ["lost"]), ("a", ["one", "two"]), ("broken", ["x"]))
    ]
    # A line it cannot read is one more failure; the drops after it still run.
    assert dropper.serve([lines[0], "{not json\n", *lines[1:]], drop) == 1
    assert dropped == [("a", ["one", "two"])]
    errors = capsys.readouterr().err
    assert errors.splitlines()[0::2] == ["dropping ['lost']: refused", "dropping ['x']: refused"]
    assert errors.splitlines()[1].startswith("dropping an unreadable line: Expecting")
    assert "secret" not in errors
    assert dropper.serve(lines[1:2], drop) == 0


def test_the_controllers_session_end_waits_for_its_own_drops(monkeypatch):
    waited = []
    monkeypatch.setattr(helpers, "drop_queued", lambda: waited.append(True))
    monkeypatch.setattr(conftest, "SHARED_OWNER", False)
    conftest.pytest_sessionfinish(None)
    assert waited == [True]
    # trylast: after pytest's own pytest_sessionfinish.
    assert conftest.pytest_sessionfinish.pytest_impl["trylast"] is True


FAILING = """
import helpers
from test_storage import admin_credentials


def test_queues_a_drop_that_fails():
    helpers.drop_databases(admin_credentials(), ["proxy_test_never_created"])
"""


def test_a_failed_drop_fails_an_xdist_run(tmp_path):
    # The real path: a worker's failed drop must fail the run, not vanish in xdist's own
    # pytest_sessionfinish (conftest.wait_for_drops).
    admin_credentials()
    (tmp_path / "test_failing_drop.py").write_text(FAILING)
    tests = Path(__file__).resolve().parent
    inherited = ("PYTEST_XDIST", "CI_TIMELINE", "COV_CORE", "COVERAGE")
    env = {k: v for k, v in os.environ.items() if not k.startswith(inherited)}
    env["PYTHONPATH"] = os.pathsep.join([str(tests), str(tests.parent / "src")])
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "conftest", "-p", "no:cacheprovider", "-n", "1"]
        + ["-q", "test_failing_drop.py"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "1 passed, 1 error" in result.stdout
    assert "DropError" in result.stdout and "proxy_test_never_created" in result.stdout


def test_a_queued_database_is_dropped_by_the_session_end(monkeypatch, tmp_path):
    admin = admin_credentials()
    monkeypatch.setattr(dropper, "PROCESS", None)
    monkeypatch.setenv("EV", str(tmp_path))
    monkeypatch.setenv("CI_TIMELINE", "1")
    names = [helpers.scratch_database(admin) for _ in range(2)]
    helpers.drop_databases(admin, names)
    helpers.drop_queued()
    query = "SELECT count(*) FROM pg_database WHERE datname = ANY(%s)"
    with psycopg.connect(**admin, autocommit=True) as conn:
        assert conn.execute(query, (names,)).fetchone()[0] == 0
    # The dropper's own timeline: the database count before the drop, then the drop.
    path = tmp_path / f"timeline-{helpers.WORKER}.jsonl"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    count, span = (r for r in records if r["kind"] in ("databases", "drop_database"))
    assert count["count"] >= 2 and span["databases"] == 2
    # A drop that fails (the databases are gone) fails the session end.
    helpers.drop_databases(admin, names[:1])
    with pytest.raises(dropper.DropError, match="does not exist"):
        helpers.drop_queued()
