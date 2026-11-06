"""Shared helpers and fixtures of the orchestrator tests (test_orchestrator_*.py).

Seeded reference data, SQL reads, an orchestrator database pool and API client, node
tokens as headers, settings, a planned census run and hand-built result archives.
"""

import io
import tarfile
from datetime import UTC, datetime

import pytest
from aiohttp.test_utils import TestClient, TestServer

from proxy_index.orchestrator import batches
from proxy_index.orchestrator.app import build_app
from proxy_index.orchestrator.db import Database, put_setting
from proxy_index.orchestrator.nodes import issue_token
from proxy_index.regions import REGION_IDS
from proxy_index.storage import connect, seed

NOW = datetime(2026, 10, 1, 11, 59, 30, tzinfo=UTC)
FRA1, LON1 = REGION_IDS["fra1"], REGION_IDS["lon1"]


def prepare(config, countries=("DE",), **times):
    """Seeded reference data with only `countries` enabled; times maps code -> census time."""
    seed(config)
    with connect(config) as conn:
        conn.execute("UPDATE countries SET enabled = country_code = ANY(%s)", (list(countries),))
        for code, value in times.items():
            conn.execute("UPDATE countries SET census_time=%s WHERE country_code=%s", (value, code))


def query(config, statement, params=None):
    with connect(config) as conn:
        return conn.execute(statement, params).fetchall()


def product_id(config, provider, product="res"):
    return query(
        config, "SELECT product_id FROM products WHERE provider=%s AND name=%s", (provider, product)
    )[0][0]


@pytest.fixture
async def db(database):
    pool = Database(database)
    yield pool
    await pool.close()


@pytest.fixture
async def client(database, db):
    app = build_app(database, with_scheduler=False, db=db)
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    yield test_client
    await test_client.close()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def set_setting(db, key, value):
    async with db.connection() as conn:
        await put_setting(conn, key, value)


async def planned_run(database, db, client, *, provider="fixture", product="residential"):
    """A census batch for DE with one node (fixture/residential in fra1) already running it."""
    now = datetime.now(UTC)
    prepare(database, ("DE",), DE=now.time())
    node = issue_token(database, provider=provider, product=product, region="fra1")
    await batches.census_plan(db, now)
    batch_id = query(database, "SELECT batch_id FROM test_batches")[0][0]
    headers = bearer(node["token"])
    await client.get("/v1/work", headers=headers)
    return batch_id, headers


def archive_bytes(members):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for info, data in members:
            archive.addfile(info, io.BytesIO(data) if data is not None else None)
    return buffer.getvalue()
