"""The planners never wait for a calculation (BROWSER-PLAN-LOCK-1): they take their own advisory
lock, batches.PLAN_LOCK, not the one a calculation holds for its whole transaction (minutes)."""

import asyncio
from datetime import time, timedelta

import pytest
from orchestrator_browser_cases import HOUR, NOON, browser_nodes, configure, settings_file
from orchestrator_cases import prepare, query

from atlas import storage_migrate
from atlas.calculated import rollups
from atlas.orchestrator import batches
from atlas.orchestrator.browser import browser_plan
from atlas.orchestrator.db import Database
from atlas.orchestrator.nodes import issue_token
from atlas.storage import connect

pytest_plugins = ["test_storage"]
# Far longer than a plan takes; before the fix a planner waited here until the calculation ended.
LIMIT_S = 20


@pytest.fixture
async def db(database):
    pool = Database(database)
    yield pool
    await pool.close()


def calculating(database):
    """A connection in the middle of a long calculation: its transaction holds the lock."""
    conn = connect(database)
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (rollups.LOCK,))
    return conn


async def test_browser_plan_dispatches_while_a_calculation_runs(database, db, tmp_path):
    prepare(database)
    nodes = browser_nodes(database)
    await configure(db, settings_file(tmp_path))
    with calculating(database):
        result = await asyncio.wait_for(browser_plan(db, HOUR), LIMIT_S)
        queued = query(
            database,
            "SELECT DISTINCT r.node_id FROM batch_runs r JOIN test_batches b USING(batch_id) "
            "WHERE b.kind='browser' AND r.status='queued' ORDER BY 1",
        )
    assert result["created"] == 9 and result["networks"] == 2
    assert queued == sorted((nodes[network]["node_id"],) for network in ("alpha", "beta"))


async def test_census_and_pulse_plan_while_a_calculation_runs(database, db):
    prepare(database, ("DE",), DE=time(12))
    issue_token(database, provider="alpha", product="res", region="fra1")
    with calculating(database):
        census = await asyncio.wait_for(batches.census_plan(db, NOON), LIMIT_S)
        pulse_at = NOON + timedelta(seconds=30)
        pulse = await asyncio.wait_for(batches.pulse_plan(db, pulse_at), LIMIT_S)
    assert census["created"] and pulse["created"]


def test_the_planners_lock_is_not_the_calculations_and_a_migration_holds_both(database):
    assert batches.PLAN_LOCK != rollups.LOCK
    with connect(database) as migration, connect(database) as other:
        storage_migrate.read_versions(migration)
        for lock in (batches.PLAN_LOCK, rollups.LOCK):
            row = other.execute("SELECT pg_try_advisory_xact_lock(%s)", (lock,)).fetchone()
            assert row == (False,)
