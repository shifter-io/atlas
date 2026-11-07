"""CI-90-6: a test's teardown queues its scratch databases' drops for a dropper thread."""

import json
import threading

import conftest
import helpers
import psycopg
import pytest
from test_storage import admin_credentials


class Connection:
    """A fake admin connection: records each statement and its thread, answers a count."""

    def __init__(self, log, admin, release):
        self.log, self.admin, self.release = log, admin, release

    def __enter__(self):
        self.release.wait(5)
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, *args):
        text = statement if isinstance(statement, str) else statement.as_string(None)
        self.log.append((self.admin["user"], threading.current_thread().name, text))
        return self

    def fetchone(self):
        return (42,)


@pytest.fixture
def connections(monkeypatch):
    """The statements the dropper runs; it waits for `release` before each connection."""
    log, release = [], threading.Event()
    monkeypatch.setattr(helpers, "DROPPER", None)
    monkeypatch.setattr(helpers, "DROPS", [])
    monkeypatch.setattr(helpers, "TIMELINE", False)
    monkeypatch.setattr(
        helpers.psycopg, "connect", lambda autocommit, **admin: Connection(log, admin, release)
    )
    yield log, release
    release.set()
    helpers.drop_queued()


def test_a_drop_runs_in_the_dropper_thread_and_the_session_end_waits(connections):
    log, release = connections
    helpers.drop_databases({"user": "a"}, ["one", "two"])
    helpers.drop_databases({"user": "b"}, ["three"])
    # The teardown returned before any drop ran.
    assert log == [] and len(helpers.DROPS) == 2
    release.set()
    helpers.drop_queued()
    assert helpers.DROPPER is None and helpers.DROPS == []
    assert [(user, text) for user, _, text in log] == [
        ("a", 'DROP DATABASE "one" WITH (FORCE)'),
        ("a", 'DROP DATABASE "two" WITH (FORCE)'),
        ("b", 'DROP DATABASE "three" WITH (FORCE)'),
    ]
    assert {thread for _, thread, _ in log} == {"drop-databases_0"}
    # Nothing queued: nothing to wait for; a later drop starts a new thread.
    helpers.drop_queued()
    helpers.drop_databases({"user": "a"}, ["four"])
    helpers.drop_queued()
    assert log[-1][2] == 'DROP DATABASE "four" WITH (FORCE)'


def test_the_timeline_samples_the_database_count_before_each_drop(
    connections, monkeypatch, tmp_path
):
    log, release = connections
    monkeypatch.setenv("EV", str(tmp_path))
    monkeypatch.setattr(helpers, "TIMELINE", True)
    release.set()
    helpers.drop_databases({"user": "a"}, ["one", "two"])
    helpers.drop_queued()
    assert [text for _, _, text in log][0] == "SELECT count(*) FROM pg_database"
    path = tmp_path / f"timeline-{helpers.WORKER}.jsonl"
    count, span = (json.loads(line) for line in path.read_text().splitlines())
    assert count["kind"] == "databases" and count["count"] == 42
    assert span["kind"] == "drop_database" and span["databases"] == 2


def test_a_failed_drop_fails_the_session_end_after_the_others(connections, monkeypatch):
    log, release = connections
    release.set()

    def connect(autocommit, **admin):
        if admin["user"] == "broken":
            raise psycopg.OperationalError("refused")
        return Connection(log, admin, release)

    monkeypatch.setattr(helpers.psycopg, "connect", connect)
    helpers.drop_databases({"user": "broken"}, ["lost"])
    helpers.drop_databases({"user": "a"}, ["kept"])
    with pytest.raises(psycopg.OperationalError, match="refused"):
        helpers.drop_queued()
    assert [text for _, _, text in log] == ['DROP DATABASE "kept" WITH (FORCE)']
    assert helpers.DROPS == []


def test_the_session_end_waits_for_the_drops_after_the_session_fixtures(monkeypatch):
    waited = []
    monkeypatch.setattr(helpers, "drop_queued", lambda: waited.append(True))
    monkeypatch.setattr(conftest, "SHARED_OWNER", False)
    conftest.pytest_sessionfinish(None)
    assert waited == [True]
    # trylast: after pytest's own pytest_sessionfinish, which tears the session fixtures down.
    assert conftest.pytest_sessionfinish.pytest_impl["trylast"] is True


def test_a_queued_database_is_dropped_by_the_session_end(monkeypatch):
    admin = admin_credentials()
    monkeypatch.setattr(helpers, "DROPPER", None)
    monkeypatch.setattr(helpers, "DROPS", [])
    name = helpers.scratch_database(admin)
    helpers.drop_databases(admin, [name])
    helpers.drop_queued()
    query = "SELECT count(*) FROM pg_database WHERE datname=%s"
    with psycopg.connect(**admin, autocommit=True) as conn:
        assert conn.execute(query, (name,)).fetchone()[0] == 0
