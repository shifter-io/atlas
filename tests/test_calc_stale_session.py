"""CALC-STALE-SESSION-1: a calculation whose host vanished cannot hold its locks for hours.

On 25 Nov 2026 a hard reset of the production server left the 05:06 calculation's session idle
in transaction, holding rollups.LOCK, for almost 2 hours; the 06:04 calculation waited 56 minutes
on it. Every framework session now carries keepalives and an idle-in-transaction bound, and the
build's waits for its lock end, past run_lock.WAIT, in LockWaitTimeout, which fails the job.
"""

from datetime import UTC, datetime, timedelta

import pytest

from atlas.calculated import rollups, run_lock
from atlas.cli import main
from atlas.providers.dbconnect import KEEPALIVES, session_options
from atlas.storage import connect

pytest_plugins = ["test_storage", "calculated_template", "orchestrator_cases"]


def test_the_session_options_add_the_keepalives_to_pgoptions(monkeypatch):
    monkeypatch.delenv("PGOPTIONS", raising=False)
    options = session_options("client_connection_check_interval=10s")
    assert options["keepalives_idle"] == "60" and options["tcp_user_timeout"] == "300000"
    assert options["options"].split(" -c ") == [
        "-c tcp_keepalives_idle=60",
        "tcp_keepalives_interval=10",
        "tcp_keepalives_count=6",
        "idle_in_transaction_session_timeout=3h",
        "client_connection_check_interval=10s",
    ]
    monkeypatch.setenv("PGOPTIONS", "-c search_path=scale")  # kept, as the scale tools need
    assert session_options()["options"].startswith("-c search_path=scale -c tcp_keepalives")


def server_settings(conn):
    names = ("idle_in_transaction_session_timeout", "client_connection_check_interval")
    return [conn.execute(f"SHOW {name}").fetchone()[0] for name in names]


def test_a_calculations_session_carries_the_keepalives(database):
    with connect(database) as conn:
        parameters = conn.info.get_parameters()
        assert {key: parameters[key] for key in KEEPALIVES} == KEEPALIVES
        assert server_settings(conn) == ["3h", "10s"]
        # The server's own probes; a Unix socket has none to set, so it shows 0 there.
        expected = "0" if conn.info.host.startswith("/") else "60"
        assert conn.execute("SHOW tcp_keepalives_idle").fetchone()[0] == expected


async def test_the_orchestrators_sessions_carry_the_keepalives(db):
    async with db.connection() as conn:
        assert conn.info.get_parameters()["keepalives_count"] == "6"
        row = await (await conn.execute("SHOW idle_in_transaction_session_timeout")).fetchone()
        assert row == {"idle_in_transaction_session_timeout": "3h"}


@pytest.fixture
def stuck(calculated_database, monkeypatch):
    """A database where a session left by a reset host holds the calculation's lock; the wait
    is 200 ms."""
    monkeypatch.setattr(run_lock, "WAIT", "200ms")
    with connect(calculated_database) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (rollups.LOCK,))
        yield calculated_database


def test_the_wait_for_a_held_lock_ends_in_a_typed_error(stuck):
    with connect(stuck) as conn, pytest.raises(run_lock.LockWaitTimeout, match="200ms"):
        run_lock.wait(conn, rollups.LOCK)


def test_a_free_lock_is_taken_and_the_sessions_lock_timeout_is_back(database, monkeypatch):
    monkeypatch.setenv("PGOPTIONS", "-c lock_timeout=7s")
    with connect(database) as conn:
        run_lock.wait(conn, rollups.LOCK)
        assert conn.execute("SHOW lock_timeout").fetchone()[0] == "7s"
        held = "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND pid=pg_backend_pid()"
        assert conn.execute(held).fetchone()[0] == 1


def test_the_calculations_rollups_stop_waiting_for_a_stuck_session(stuck):
    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    with pytest.raises(run_lock.LockWaitTimeout):
        rollups.fill(stuck, end)


def test_the_calculation_fails_with_the_reason_the_job_alerts_with(stuck, capsys):
    argv = ["calculate", "--database-file", str(stuck), "--methodology"]
    assert main([*argv, "configs/methodology.toml"]) == 2  # jobs: CalculationFailed, job_failed
    assert "waited 200ms for the calculation's database lock" in capsys.readouterr().err
