"""INGEST-HANG-1: an import's session never waits for ever on its own client, holding its locks.

26 Nov 2026: a daily group's session stayed `active`, waiting for the rest of a statement
(ClientRead), while its client waited to read: psycopg's pipeline sync had stopped sending with
the end of a large statement still in libpq's buffer. Every upload of its networks waited behind
its locks. Here: the sync that sends everything first, the server-side bounds of a worker
session, the deadline that kills a worker process, and the bounded lock wait of an upload.
"""

from __future__ import annotations

import threading
import time

import psycopg
import pytest
from ingest_cases import pulse, run_id

from atlas import sizing, storage, storage_batch, storage_parallel
from atlas.storage import connect
from atlas.storage_parallel import Group

pytest_plugins = ["test_storage"]

KEY = 817452990  # an advisory lock no import takes
PAYLOAD = 64 << 20  # far more than the socket buffers hold: the statement's end stays in libpq's
HERE = "database=(SELECT oid FROM pg_database WHERE datname=current_database())"
ADVISORY = f"SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND {HERE}"


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(storage_parallel, "BACKOFF", 0.01)
    monkeypatch.setattr(sizing, "reserves", lambda conn: (0, 0.0, 0))


def until(check, seconds=30.0):
    """Wait until check() is true; whether it was."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.05)
    return False


def pipelined(conn, finish, found):
    """A transaction in pipeline mode, as storage._store sends one: a statement that waits on
    KEY (held by the test), then one of PAYLOAD bytes, then finish(conn, pipeline), the sync."""
    conn.autocommit = True
    try:
        with conn.pipeline() as pipeline:
            conn.execute("BEGIN")
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (KEY,))
            length = conn.execute("SELECT length(%s)", ("x" * PAYLOAD,))
            finish(conn, pipeline)
            found.append(length.fetchone()[0])
            conn.execute("COMMIT")
    except psycopg.Error as error:
        found.append(error)


def blocked_sync(database, finish, *settings):
    """Run pipelined() in a thread while KEY is held, then free KEY: what the thread found and
    the states its session went through, and the thread (joined for at most 30 s)."""
    found, states = [], set()
    with connect(database) as blocker, connect(database, *settings) as conn:
        blocker.autocommit = True
        blocker.execute("SELECT pg_advisory_lock(%s)", (KEY,))
        pid = conn.info.backend_pid
        worker = threading.Thread(target=pipelined, args=(conn, finish, found), daemon=True)
        worker.start()
        state = "SELECT state,wait_event FROM pg_stat_activity WHERE pid=%s"
        assert until(lambda: blocker.execute(state, (pid,)).fetchone() == ("active", "advisory"))
        time.sleep(0.5)  # the client fills the socket's buffers and waits to send the rest
        blocker.execute("SELECT pg_advisory_unlock(%s)", (KEY,))
        end = time.monotonic() + 30
        while worker.is_alive() and time.monotonic() < end:
            states.add(blocker.execute(state, (pid,)).fetchone())
            worker.join(0.2)
        if worker.is_alive():  # hung: end its session so the thread ends
            blocker.execute("SELECT pg_terminate_backend(%s)", (pid,))
            worker.join(30)
            return ["hung"], states
    return found, states


def test_a_large_statement_is_sent_whole_before_the_sync_waits(database):
    """storage_batch.sync: the statement's end and the Sync reach the server, which answers."""
    found, _ = blocked_sync(database, storage_batch.sync)
    assert found == [PAYLOAD]


def test_psycopg_s_own_sync_hangs_there_and_the_transaction_timeout_ends_it(database):
    """The cause, as in production: the server waits in ClientRead mid-statement, the client to
    read. A worker session's transaction_timeout (storage_parallel.SESSION) ends it. When this
    fails because the sync no longer hangs, psycopg is fixed and storage_batch.sync can go."""
    found, states = blocked_sync(database, lambda conn, p: p.sync(), "transaction_timeout=5s")
    assert ("active", "ClientRead") in states
    assert len(found) == 1 and isinstance(found[0], psycopg.Error), found


def test_a_worker_session_bounds_its_transactions_on_the_server(database):
    connections = storage_parallel.Connections(database)
    try:
        conn = connections.get()
        assert conn.execute("SHOW transaction_timeout").fetchone() == ("10min",)
        assert conn.execute("SHOW idle_in_transaction_session_timeout").fetchone() == ("5min",)
    finally:
        connections.close()


def two_networks(tmp_path):
    return [
        Group([pulse(tmp_path / run_id("pulse", n), n, provider=name)], networks=frozenset({name}))
        for n, name in ((0, "net-a"), (5, "net-b"))
    ]


def held(database, network):
    """A session holding the network's import lock, as a group stuck in its transaction does."""
    blocker = connect(database)
    blocker.autocommit = True
    blocker.execute("SELECT pg_advisory_lock(%s,hashtext(%s))", (storage.NETWORK_LOCK, network))
    return blocker


def test_a_group_past_its_deadline_has_its_process_killed_and_its_locks_freed(
    database, tmp_path, capsys
):
    """net-a's group stops answering mid-transaction (it waits here on its network's lock, past
    the deadline): its worker process is killed, the group fails named, its session and locks
    go; net-b's group, in the other process, commits."""
    a, b = two_networks(tmp_path)
    storage.ingest(pulse(tmp_path / "first", 2), database)  # the day's partitions
    options = storage_parallel.Options(connections=2, processes=2, deadline=10)
    blocker = held(database, "net-a")
    others = f"{ADVISORY} AND pid<>pg_backend_pid()"
    try:
        with storage_parallel.Pool(database, options) as pool:
            began = time.monotonic()
            outcomes = pool.wait([a, b], pool.submit([a, b]))
            took = time.monotonic() - began
        assert until(lambda: blocker.execute(others).fetchone()[0] == 0)
    finally:
        blocker.close()
    assert outcomes[0].error.run_ids == [run_id("pulse", 0)]
    assert "died" in str(outcomes[0].error.__cause__) and outcomes[1].error is None
    assert 10 <= took < 40
    err = capsys.readouterr().err
    assert f"a group passed its deadline (10 s): ['{run_id('pulse', 0)}']" in err
    assert f"worker process died (-9): ['{run_id('pulse', 0)}']" in err


def test_a_thread_lane_s_group_past_its_deadline_is_named_once_and_left_to_its_session(
    database, tmp_path, capsys
):
    """P = 1: a thread cannot be killed. The group is named once (no busy loop after it) and,
    once its lock is free, commits."""
    a, _ = two_networks(tmp_path)
    storage.ingest(pulse(tmp_path / "first", 2), database)
    blocker = held(database, "net-a")
    with storage_parallel.Pool(database, storage_parallel.Options(1, deadline=0.5)) as pool:
        tickets = pool.submit([a])
        time.sleep(2)
        blocker.close()
        outcomes = pool.wait([a], tickets)
    assert outcomes[0].error is None
    assert capsys.readouterr().err.count("passed its deadline") == 1


def test_an_upload_s_import_waits_for_a_lock_at_most_its_lock_wait(database, tmp_path):
    """storage.ingest(lock_wait=): past it LockNotAvailable (an OperationalError: 503 at the
    upload) and nothing stored; with the lock free the same import goes in."""
    storage.ingest(pulse(tmp_path / "first", 2), database)
    directory = pulse(tmp_path / "late", 3, provider="net-a")
    blocker = held(database, "net-a")
    try:
        began = time.monotonic()
        with pytest.raises(psycopg.errors.LockNotAvailable):
            storage.ingest(directory, database, lock_wait="1s")
        assert time.monotonic() - began < 15
        assert isinstance(psycopg.errors.LockNotAvailable("x"), psycopg.OperationalError)
    finally:
        blocker.close()
    with connect(database) as conn:
        assert conn.execute(ADVISORY).fetchone()[0] == 0
        stored = "SELECT count(*) FROM test_runs WHERE run_id=%s::uuid"
        assert conn.execute(stored, (run_id("pulse", 3),)).fetchone()[0] == 0
    assert storage.ingest(directory, database, lock_wait="1s")["imported"]
