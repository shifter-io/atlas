"""The scheduler's clock (orchestrator/scheduler.py) on an in-memory pool: fire times later in the
same hour or day, a database that is down, start and stop, and a stop that cancels a running job.
"""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta

import pytest

from proxy_index.orchestrator.scheduler import Scheduler, Task

AT = datetime(2026, 10, 1, 12, 0, 30, tzinfo=UTC)


class Rows:
    def __init__(self, row):
        self.row = row

    async def fetchone(self):
        return self.row


class Connection:
    def __init__(self, pool):
        self.pool = pool

    async def execute(self, statement, params=None):
        self.pool.statements.append((statement.split("(", 1)[0], params))
        return Rows({"id": len(self.pool.statements)})


class Pool:
    """Records every statement; with `down`, every connection attempt fails as a lost database."""

    def __init__(self, *, down=False):
        self.down = down
        self.statements = []

    @contextlib.asynccontextmanager
    async def connection(self):
        if self.down:
            raise OSError("connection refused")
        yield Connection(self)

    transaction = connection


async def idle():
    return None


def test_fire_times_later_in_the_same_hour_and_day():
    assert Task("m", idle, at_minute=45).schedule(AT) == AT.replace(minute=45, second=0)
    assert Task("d", idle, at_utc_minute=13 * 60).schedule(AT) == datetime(
        2026, 10, 1, 13, tzinfo=UTC
    )
    # The exact fire time is already past: the next hour, the next day.
    on_time = AT.replace(second=0)
    assert Task("m", idle, at_minute=0).schedule(on_time) == on_time + timedelta(hours=1)
    assert Task("d", idle, at_utc_minute=12 * 60).schedule(on_time) == on_time + timedelta(days=1)
    # No interval: once a minute from now.
    assert Task("e", idle).schedule(AT) == AT + timedelta(minutes=1)


async def test_a_database_that_is_down_does_not_stop_the_clock(caplog):
    scheduler = Scheduler(Pool(down=True))
    calls = []

    async def job():
        calls.append(1)

    scheduler.add("tick", job, every_s=60)
    with caplog.at_level(logging.ERROR, logger="proxy_index.orchestrator.scheduler"):
        await scheduler._fire(scheduler.tasks["tick"])
    # The run could not be recorded, so the job did not run, and the task is free again.
    assert calls == []
    assert scheduler.tasks["tick"].running is False
    assert "could not record job tick" in caplog.text


async def test_start_is_idempotent_and_an_empty_clock_stops_cleanly():
    scheduler = Scheduler(Pool())
    scheduler.start()
    runner = scheduler._runner
    scheduler.start()
    assert scheduler._runner is runner
    await asyncio.sleep(0)
    await scheduler.stop()
    assert scheduler._runner is None and runner.cancelled()
    # Stopping a stopped clock is a no-op.
    await scheduler.stop()
    assert scheduler.upcoming() == []


async def test_stop_cancels_a_job_still_running():
    pool = Pool()
    scheduler = Scheduler(pool)
    started, outcome = asyncio.Event(), []

    async def slow():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            outcome.append("cancelled")
            raise

    scheduler.add("slow", slow, every_s=0.01)
    scheduler.start()
    await asyncio.wait_for(started.wait(), 5)
    (entry,) = scheduler.upcoming()
    assert entry["job"] == "slow" and entry["running"] and entry["next_at"] is not None
    await scheduler.stop()
    assert outcome == ["cancelled"]
    await asyncio.sleep(0)  # the fired tasks' done callbacks
    assert scheduler._fired == set() and scheduler.tasks["slow"].running is False
    # The run was recorded as started; a cancelled run is never marked finished.
    assert pool.statements[0] == ("INSERT INTO job_runs", ("slow",))
    assert not any(s.startswith("UPDATE") for s, _ in pool.statements)


@pytest.mark.parametrize("stats", [None, {"skipped": 3}, {"skipped": ""}])
async def test_only_a_reason_string_marks_a_skip(stats):
    scheduler = Scheduler(Pool())

    async def job():
        return stats

    scheduler.add("plan", job)
    assert await scheduler.run_now("plan") == stats
    assert scheduler.tasks["plan"].last["status"] == "ok"
