"""Shared fixtures and helpers of the admin JSON API tests (test_admin*.py)."""

import base64
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer
from orchestrator_cases import prepare, query

from atlas.orchestrator import batches, nodework
from atlas.orchestrator.admin import views_dashboard
from atlas.orchestrator.app import build_app
from atlas.orchestrator.db import Database
from atlas.orchestrator.nodes import issue_token
from atlas.regions import REGION_IDS

CREDENTIAL = "ops:s3cret"
AUTH = {"Authorization": "Basic " + base64.b64encode(CREDENTIAL.encode()).decode()}
FRA1 = REGION_IDS["fra1"]


@pytest.fixture
async def db(database):
    pool = Database(database)
    yield pool
    await pool.close()


async def serve(app):
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.fixture
async def admin(database, db):
    client = await serve(
        build_app(database, with_scheduler=False, db=db, admin_credential=CREDENTIAL)
    )
    yield client
    await client.close()


async def get(client, path, expect=200, **params):
    reply = await client.get(f"/admin/api{path}", headers=AUTH, params=params)
    assert reply.status == expect, await reply.text()
    return await reply.json()


async def post(client, path, body=None, expect=200):
    reply = await client.post(f"/admin/api{path}", json=body or {}, headers=AUTH)
    assert reply.status == expect, await reply.text()
    return await reply.json()


async def census(database, db, *countries, providers=("alpha", "beta")):
    """A census batch for DE (fra1) with one node per provider, alpha running, beta queued."""
    now = datetime.now(UTC)
    prepare(database, countries or ("DE",), DE=now.time())
    tokens = {p: issue_token(database, provider=p, product="res", region="fra1") for p in providers}
    await batches.census_plan(db, now)
    batch_id = query(database, "SELECT batch_id FROM test_batches WHERE kind='census'")[0][0]
    alpha = {"node_id": tokens["alpha"]["node_id"]}
    await nodework.assignments(db, alpha, now + timedelta(minutes=1))
    counters = {
        "requests": 120,
        "successes": 100,
        "unique_ips": 80,
        "bytes_up": 1000,
        "bytes_down": 9000,
    }
    await nodework.progress(db, alpha, batch_id, counters)
    return batch_id, tokens


async def plan_pulses(db, monkeypatch=None):
    """Plan the current pulse slot from its start and return the wall-clock instant used.

    A slot opened more than pulse.GRACE late spreads over its rest only, and a target whose
    start no longer fits waits for the next slot: planned in a slot's last 30 seconds, the
    second target got no batch. Planning from the slot start opens both at any time of day.
    With monkeypatch, the admin readers' clock is frozen at the returned instant, so the
    planned slot is their current one even if a slot boundary passes during the test.
    """
    now = datetime.now(UTC)
    step = 30 * 60  # the targets' default pulse_interval_minutes
    await batches.pulse_plan(db, datetime.fromtimestamp(now.timestamp() // step * step, UTC))
    if monkeypatch is not None:
        monkeypatch.setattr(
            views_dashboard, "_now", lambda value=None: (value or now).astimezone(UTC)
        )
    return now
