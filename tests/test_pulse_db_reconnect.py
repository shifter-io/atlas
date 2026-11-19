"""PULSE-DB-RECONNECT-1: a dropped database connection (the server restarted: AdminShutdown,
57P01) never fails a whole pulse hour. Its steps are called again on a new connection for about
60 s (orchestrator.db.reconnecting), and the pool closes its other idle connections, which the
same restart dropped. The connections are really dropped (pg_terminate_backend); the database
is the test's own, the cloud and SSH are fakes."""

from __future__ import annotations

import asyncio
import json
import logging

import psycopg
import pytest
from psycopg import errors
from pulse_fleet_cases import world
from test_pulse_ondemand import HourClock, hour, ready

from atlas.campaign import pulse_replace
from atlas.campaign.pulse_ondemand import WAIT_POLL_SECONDS
from atlas.orchestrator.db import (
    RECONNECT_SECONDS,
    connection_dropped,
    reconnecting,
    setting,
)
from atlas.storage import connect

pytest_plugins = ["test_storage", "orchestrator_cases"]

TERMINATE = (
    "SELECT count(pg_terminate_backend(pid, 5000)) FROM pg_stat_activity "
    "WHERE datname=current_database() AND usename=current_user "
    "AND backend_type='client backend' AND pid<>pg_backend_pid()"
)


def drop_connections(database) -> int:
    """Terminate every other session of the test's database, as a server restart does."""
    with connect(database) as conn:
        return conn.execute(TERMINATE).fetchone()[0]


class Clock:
    def __init__(self):
        self.t, self.slept = 0.0, []

    def now(self):
        return self.t

    async def sleep(self, seconds):
        self.t += seconds
        self.slept.append(seconds)


class SleepClock(HourClock):
    """The hour's clock, recording each sleep."""

    def __init__(self, database):
        super().__init__(database)
        self.slept = []

    async def sleep(self, seconds):
        self.slept.append(seconds)
        await super().sleep(seconds)


def down_for(db, times: int | None) -> list:
    """The server refuses the pool's next `times` connections (None: every one)."""
    refused, real = [], db._open

    async def refusing():
        if times is None or len(refused) < times:
            refused.append(1)
            raise psycopg.OperationalError("connection failed: Connection refused")
        return await real()

    db._open = refusing
    return refused


def test_a_dropped_connection_is_told_from_a_refused_statement():
    assert connection_dropped(errors.AdminShutdown("terminating connection"))
    assert connection_dropped(errors.CrashShutdown())
    assert connection_dropped(errors.CannotConnectNow())
    assert connection_dropped(errors.ConnectionFailure())  # class 08
    assert connection_dropped(psycopg.OperationalError("server closed the connection"))
    assert not connection_dropped(errors.QueryCanceled())  # a statement timeout: not again
    assert not connection_dropped(errors.UniqueViolation())
    assert not connection_dropped(RuntimeError("down"))


async def test_a_step_is_called_again_with_backoff_until_it_succeeds():
    clock, calls = Clock(), []

    async def step(value):
        calls.append(value)
        if len(calls) < 4:
            raise errors.AdminShutdown("terminating connection due to administrator command")
        return value * 2

    assert await reconnecting(clock, step, 21) == 42
    assert (calls, clock.slept) == ([21] * 4, [1.0, 2.0, 4.0])


async def test_a_step_gives_up_about_60_seconds_after_its_first_failure():
    clock, calls = Clock(), []

    async def step():
        calls.append(clock.now())
        raise psycopg.OperationalError("connection failed: Connection refused")

    with pytest.raises(psycopg.OperationalError, match="refused"):
        await reconnecting(clock, step)
    assert clock.slept == [1.0, 2.0, 4.0, 8.0, 15.0, 15.0, 15.0]
    assert calls[-1] == RECONNECT_SECONDS and len(calls) == 8


async def test_any_other_error_is_raised_at_once():
    clock = Clock()

    async def step():
        raise errors.QueryCanceled("canceling statement due to statement timeout")

    with pytest.raises(errors.QueryCanceled):
        await reconnecting(clock, step)
    assert clock.slept == []


async def test_the_pool_closes_its_idle_connections_once_one_is_found_dropped(database, db):
    async def held(conn_ready, release):
        async with db.connection() as conn:
            await conn.execute("SELECT 1")
            conn_ready.set()
            await release.wait()

    first, second, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    tasks = [asyncio.create_task(held(first, release)), asyncio.create_task(held(second, release))]
    await first.wait(), await second.wait()
    release.set()
    await asyncio.gather(*tasks)
    assert len(db._idle) == 2 and drop_connections(database) >= 2
    with pytest.raises(psycopg.OperationalError) as caught:
        async with db.connection() as conn:
            await conn.execute("SELECT 1")
    assert connection_dropped(caught.value) and db._idle == []
    async with db.connection() as conn:  # a new connection: the step called again works
        assert await setting(conn, "pulse.step_seconds") == 12


async def test_a_drop_before_the_droplets_are_created_still_opens_the_hour(
    tmp_path, database, db, monkeypatch, caplog
):
    """The 18 Nov 2026 hour: the server restarted between the plan and the create; the hour's
    next database step (opening its pulses) found its connection gone, and the server still
    refused two connections."""
    await ready(database, db)
    real = pulse_replace.arm

    async def arm(*args):
        drop_connections(database)
        down_for(db, 2)
        return await real(*args)

    monkeypatch.setattr(pulse_replace, "arm", arm)
    fake, clock = world(), SleepClock(database)
    with caplog.at_level(logging.WARNING, "atlas.orchestrator.db"):
        _, outcome = await hour(tmp_path, fake, db, clock)
    assert (outcome["state"], outcome["armed"], outcome["leftover"]) == ("done", 2, [])
    assert (outcome["opened"]["created"], outcome["opened"]["runs"]) == (4, 4)
    assert len(fake.deleted) == 2 and "error" not in outcome
    assert clock.slept[:3] == [1.0, 2.0, 4.0]  # the dropped connection, then two refused
    assert [r.getMessage().split(":")[0] for r in caplog.records] == ["open_hour"] * 3


async def test_a_drop_after_the_droplets_are_created_and_in_the_wait_still_opens_the_hour(
    tmp_path, database, db, monkeypatch
):
    await ready(database, db)
    real = pulse_replace.arm

    async def arm(*args):
        found = await real(*args)  # created and armed
        drop_connections(database)
        return found

    class DroppingClock(SleepClock):
        async def sleep(self, seconds):  # the wait's first poll is done: drop again
            if seconds == WAIT_POLL_SECONDS and WAIT_POLL_SECONDS not in self.slept:
                drop_connections(database)
            await super().sleep(seconds)

    monkeypatch.setattr(pulse_replace, "arm", arm)
    fake, clock = world(), DroppingClock(database)
    _, outcome = await hour(tmp_path, fake, db, clock)
    assert (outcome["state"], outcome["armed"], outcome["leftover"]) == ("done", 2, [])
    assert (outcome["opened"]["created"], outcome["opened"]["runs"]) == (4, 4)
    assert len(fake.deleted) == 2 and "retire_error" not in outcome
    # open_hour once, then the wait's next poll, each on a new connection after 1 s
    assert clock.slept[:3] == [1.0, WAIT_POLL_SECONDS, 1.0]


async def test_a_database_down_for_longer_gives_up_and_still_removes_every_droplet(
    tmp_path, database, db, monkeypatch
):
    await ready(database, db)
    real = pulse_replace.arm

    async def arm(*args):
        found = await real(*args)
        drop_connections(database)
        down_for(db, None)
        return found

    monkeypatch.setattr(pulse_replace, "arm", arm)
    fake, clock = world(), SleepClock(database)
    with pytest.raises(psycopg.OperationalError):
        await hour(tmp_path, fake, db, clock)
    assert clock.slept[:7] == [1.0, 2.0, 4.0, 8.0, 15.0, 15.0, 15.0]  # open_hour's 60 s
    assert len(fake.deleted) == 2  # given up: every droplet still removed
    written = json.loads((tmp_path / "fleet/hours/20261001T12/pulse.json").read_text())
    assert (written["state"], written["error"], written["leftover"]) == (
        "failed",
        "OperationalError",
        [],
    )
    assert written["retire_error"] == written["close_error"] == "OperationalError"
