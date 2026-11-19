"""CALC-LOCK-1: a second calculation while one runs is refused, in every way one starts."""

import fcntl
import logging
import sys
from datetime import UTC, datetime

import pytest
from orchestrator_cases import query
from test_cov_orchestrator_scheduler import Pool

from atlas.calculated import run_lock, score_backfill
from atlas.calculated.calculator import calculate
from atlas.cli import main
from atlas.orchestrator import jobs
from atlas.orchestrator.scheduler import Scheduler
from atlas.storage import connect

pytest_plugins = ["test_storage", "orchestrator_cases"]

AS_OF = datetime(2026, 11, 20, 15, 10, tzinfo=UTC)


@pytest.fixture
def running(database):
    """A calculation of the database is running: it holds the run lock."""
    with connect(database) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (run_lock.KEY,))
        yield
        conn.execute("SELECT pg_advisory_unlock(%s)", (run_lock.KEY,))


def test_a_second_calculation_of_the_database_is_refused_before_it_reads(database, running):
    with pytest.raises(run_lock.Busy, match="this database"):
        calculate(database, "missing.toml")  # refused before the methodology is read


def test_the_lock_is_released_when_the_calculation_ends(database):
    with run_lock.hold(database), pytest.raises(run_lock.Busy):
        with run_lock.hold(database):
            pass
    with run_lock.hold(database):
        pass


def test_a_shells_calculation_exits_busy(database, running, capsys):
    argv = ["calculate", "--database-file", str(database), "--methodology", "missing.toml"]
    assert main(argv) == run_lock.BUSY_EXIT
    assert "this database" in capsys.readouterr().err


def test_a_backfill_point_is_refused_beside_a_calculation(database, running):
    assert issubclass(score_backfill.Busy, run_lock.Busy)  # the command exits 3 on either
    with pytest.raises(run_lock.Busy):
        score_backfill.point(database, "missing.toml", AS_OF)


def test_the_server_lock_file_refuses_a_calculation_of_another_database(
    database, tmp_path, monkeypatch
):
    path = tmp_path / "calculate.lock"
    monkeypatch.setenv(run_lock.FILE_ENV, str(path))
    with path.open("a") as other:  # staging's calculation, on its own database
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(run_lock.Busy, match="calculate.lock"):
            calculate(database, "missing.toml")
        fcntl.flock(other, fcntl.LOCK_UN)
    with run_lock.hold(database), pytest.raises(run_lock.Busy, match="on this server"):
        with run_lock.hold(database):
            pass


def test_the_server_lock_file_is_the_host_layouts(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(run_lock.FILE_ENV)
    assert run_lock.lock_file() is None  # a droplet, a test: no ~/atlas
    (tmp_path / "atlas").mkdir()
    assert run_lock.lock_file() == tmp_path / "atlas" / "calculate.lock"
    monkeypatch.setenv(run_lock.FILE_ENV, "")
    assert run_lock.lock_file() is None
    monkeypatch.setenv(run_lock.FILE_ENV, str(tmp_path / "x.lock"))
    assert run_lock.lock_file() == tmp_path / "x.lock"


async def test_a_busy_exit_is_recorded_as_a_skip():
    code = f"import sys; print('busy', file=sys.stderr); sys.exit({run_lock.BUSY_EXIT})"
    result = await jobs.run_calculation([sys.executable, "-c", code], 30)
    assert result == {"skipped": "another calculation is running"}


def systemctl(tmp_path, monkeypatch, exit_code):
    """A stand-in systemctl on PATH answering is-active with exit_code."""
    tool = tmp_path / "bin" / "systemctl"
    tool.parent.mkdir(exist_ok=True)
    tool.write_text(f'#!/bin/sh\necho "$@" > "{tmp_path}/asked"\nexit {exit_code}\n')
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(tool.parent))
    return tmp_path / "asked"


async def test_a_calculation_still_in_its_scope_skips_the_next(database, db, tmp_path, monkeypatch):
    calls = []

    async def fake(argv, limit_s):
        calls.append(argv)
        return {"snapshot_id": "s1", "as_of": "now"}

    monkeypatch.setattr(jobs, "run_calculation", fake)
    monkeypatch.setenv(jobs.MEMORY_ENV, "8G")
    monkeypatch.setenv(jobs.SCOPE_ENV, "atlas-staging-calculate")
    asked = systemctl(tmp_path, monkeypatch, 0)
    assert await jobs.calculate(db, database, "m.toml") == {"skipped": "still running"}
    assert asked.read_text().split() == [
        "--user",
        "is-active",
        "--quiet",
        "atlas-staging-calculate.scope",
    ]
    assert calls == []
    systemctl(tmp_path, monkeypatch, 3)  # inactive: the calculation starts
    assert await jobs.calculate(db, database, "m.toml") == {"snapshot_id": "s1", "as_of": "now"}
    assert len(calls) == 1


async def test_without_systemctl_the_scope_is_not_active(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert await jobs.scope_active(None) is False
    asked = systemctl(tmp_path, monkeypatch, 0)
    assert await jobs.scope_active(None) is True
    assert asked.read_text().split() == ["is-active", "--quiet", "atlas-calculate.scope"]


async def test_a_restart_fails_the_runs_left_running(database, db):
    with connect(database) as conn:
        conn.execute("INSERT INTO job_runs(job,status) VALUES ('calculate','running')")
        conn.execute("INSERT INTO job_runs(job,status,finished_at) VALUES ('detect','ok',now())")
    assert await Scheduler(db).fail_leftovers() == 1
    rows = query(database, "SELECT job,status,error,finished_at IS NOT NULL FROM job_runs")
    assert sorted(rows) == [
        ("calculate", "failed", "orchestrator restarted", True),
        ("detect", "ok", None, True),
    ]


async def test_a_restart_with_the_database_down_still_starts(caplog):
    with caplog.at_level(logging.ERROR, logger="atlas.orchestrator.scheduler"):
        assert await Scheduler(Pool(down=True)).fail_leftovers() == 0
    assert "left running" in caplog.text


def test_a_killed_calculations_session_ends_mid_query(database, monkeypatch):
    with connect(database) as conn:
        assert conn.execute("SHOW client_connection_check_interval").fetchone()[0] == "10s"
    monkeypatch.setenv("PGOPTIONS", "-c statement_timeout=7s")  # kept, as the scale tools need
    with connect(database) as conn:
        assert conn.execute("SHOW client_connection_check_interval").fetchone()[0] == "10s"
        assert conn.execute("SHOW statement_timeout").fetchone()[0] == "7s"
