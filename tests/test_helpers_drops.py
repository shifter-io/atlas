"""CI-90-6: scratch databases are dropped at each process's session end, not in each teardown."""

import json
import threading

import conftest
import helpers
import psycopg
import pytest
from test_storage import admin_credentials


class Connection:
    """A fake admin connection: records each statement, answers the database count."""

    def __init__(self, log, admin):
        self.log, self.admin = log, admin

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, *args):
        text = statement if isinstance(statement, str) else statement.as_string(None)
        self.log.append((self.admin["user"], threading.get_ident(), text))
        return self

    def fetchone(self):
        return (42,)


@pytest.fixture
def connections(monkeypatch):
    log = []
    monkeypatch.setattr(helpers, "QUEUED", [])
    monkeypatch.setattr(helpers, "TIMELINE", False)
    monkeypatch.setattr(
        helpers.psycopg, "connect", lambda autocommit, **admin: Connection(log, admin)
    )
    return log


def test_a_drop_only_queues_until_the_session_end(connections):
    helpers.drop_databases({"user": "a"}, ["one", "two"])
    assert connections == []
    assert helpers.QUEUED == [({"user": "a"}, "one"), ({"user": "a"}, "two")]
    helpers.drop_queued()
    assert helpers.QUEUED == []
    assert sorted(text for _, _, text in connections) == [
        'DROP DATABASE "one" WITH (FORCE)',
        'DROP DATABASE "two" WITH (FORCE)',
    ]
    helpers.drop_queued()
    assert len(connections) == 2


def test_the_queue_drops_on_droppers_connections_per_admin(connections, monkeypatch, tmp_path):
    monkeypatch.setenv("EV", str(tmp_path))
    monkeypatch.setattr(helpers, "TIMELINE", True)
    names = [f"db{index}" for index in range(10)]
    helpers.drop_databases({"user": "a"}, names)
    helpers.drop_databases({"user": "b"}, ["other"])
    helpers.drop_queued()
    count, *drops = connections
    assert count[2] == "SELECT count(*) FROM pg_database"
    assert sorted(text.split('"')[1] for _, _, text in drops) == sorted(names + ["other"])
    assert {user for user, _, _ in drops if user == "b"} == {"b"}
    records = [json.loads(line) for line in (tmp_path / f"timeline-{helpers.WORKER}.jsonl").open()]
    assert records[0]["kind"] == "databases" and records[0]["count"] == 42
    spans = [r for r in records if r["kind"] == "drop_database"]
    # Four shares of a's ten databases (3, 3, 2, 2) and b's one, each on its own connection.
    assert sorted(r["databases"] for r in spans) == [1, 2, 2, 3, 3]
    assert len(spans) == helpers.DROPPERS + 1


def test_a_failed_drop_fails_the_session_end_after_the_others(connections, monkeypatch):
    def connect(autocommit, **admin):
        if admin["user"] == "broken":
            raise psycopg.OperationalError("refused")
        return Connection(connections, admin)

    monkeypatch.setattr(helpers.psycopg, "connect", connect)
    helpers.drop_databases({"user": "broken"}, ["lost"])
    helpers.drop_databases({"user": "a"}, ["kept"])
    with pytest.raises(psycopg.OperationalError, match="refused"):
        helpers.drop_queued()
    assert [text for _, _, text in connections] == ['DROP DATABASE "kept" WITH (FORCE)']


def test_the_session_end_drops_the_queue_after_the_session_fixtures(monkeypatch):
    dropped = []
    monkeypatch.setattr(helpers, "drop_queued", lambda: dropped.append(True))
    monkeypatch.setattr(conftest, "SHARED_OWNER", False)
    conftest.pytest_sessionfinish(None)
    assert dropped == [True]
    # trylast: after pytest's own pytest_sessionfinish, which tears the session fixtures down.
    assert conftest.pytest_sessionfinish.pytest_impl["trylast"] is True


def test_a_queued_database_lives_until_the_queue_drops(monkeypatch):
    admin = admin_credentials()
    monkeypatch.setattr(helpers, "QUEUED", [])
    name = helpers.scratch_database(admin)
    query = "SELECT count(*) FROM pg_database WHERE datname=%s"
    helpers.drop_databases(admin, [name])
    with psycopg.connect(**admin, autocommit=True) as conn:
        assert conn.execute(query, (name,)).fetchone()[0] == 1
        helpers.drop_queued()
        assert conn.execute(query, (name,)).fetchone()[0] == 0
