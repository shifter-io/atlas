"""MANAGER-ORCH-1 (K7): the node API is the one ingest service; uploads from several nodes at once
are each ingested exactly once, spooled under unique names, and leave no temporary file."""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import UTC, datetime

import pytest
from aiohttp.test_utils import TestClient, TestServer
from orchestrator_cases import bearer, prepare, query
from test_storage import measurement

from proxy_index.orchestrator import batches
from proxy_index.orchestrator.app import build_app
from proxy_index.orchestrator.archive import pack
from proxy_index.orchestrator.db import Database, put_setting
from proxy_index.orchestrator.nodes import issue_token

pytest_plugins = ["test_storage"]

NODES = 8


@pytest.fixture
async def db(database):
    pool = Database(database)
    yield pool
    await pool.close()


@pytest.fixture
async def client(database, db):
    test_client = TestClient(TestServer(build_app(database, with_scheduler=False, db=db)))
    await test_client.start_server()
    yield test_client
    await test_client.close()


async def eight_running_runs(database, db, client, tmp_path):
    """One census batch for DE with eight networks, each node's run started; their uploads."""
    now = datetime.now(UTC)
    prepare(database, ("DE",), DE=now.time())
    tokens = [
        issue_token(database, provider=f"net{i}", product="res", region="fra1")["token"]
        for i in range(NODES)
    ]
    await batches.census_plan(db, now)
    ((batch_id,),) = query(database, "SELECT batch_id FROM test_batches")
    uploads = []
    for i, token in enumerate(tokens):
        work = await client.get("/v1/work", headers=bearer(token))
        assert work.status == 200, await work.text()
        directory = await measurement(
            tmp_path / f"node{i}", requests=3, provider=f"net{i}", product="res"
        )
        archive = pack(directory, tmp_path / f"node{i}.tar.gz").read_bytes()
        run_id = json.loads((directory / "summary.json").read_text())["run_id"]
        uploads.append((bearer(token), archive, run_id))
    return batch_id, uploads


async def test_eight_simultaneous_uploads_are_ingested_exactly_once(
    database, db, client, tmp_path, monkeypatch
):
    spool, scratch = tmp_path / "spool", tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))  # where uploads stream to
    async with db.transaction() as conn:
        await put_setting(conn, "raw.spool_dir", str(spool))
    batch_id, uploads = await eight_running_runs(database, db, client, tmp_path)

    async def send(headers, archive):
        reply = await client.post(f"/v1/results?batch_id={batch_id}", data=archive, headers=headers)
        assert reply.status == 200, await reply.text()
        return await reply.json()

    first = await asyncio.gather(*(send(headers, archive) for headers, archive, _ in uploads))
    assert all(body["ingested"] and body["imported"] for body in first)
    run_keys = {body["run_key"] for body in first}
    assert len(run_keys) == NODES
    # Every node's retry, again all at once, is acknowledged without a second import.
    again = await asyncio.gather(*(send(headers, archive) for headers, archive, _ in uploads))
    assert [body["run_key"] for body in again] == [body["run_key"] for body in first]

    assert query(database, "SELECT count(*), count(DISTINCT run_key) FROM test_runs") == [
        (NODES, NODES)
    ]
    assert query(
        database, "SELECT count(*) FROM batch_runs WHERE status='done' AND run_key IS NOT NULL"
    ) == [(NODES,)]
    assert query(database, "SELECT status FROM test_batches") == [("done",)]
    spooled = sorted(path for path in spool.rglob("*") if path.is_file())
    assert sorted(path.name for path in spooled) == sorted(
        f"census-b{batch_id}-{run_id}.tar.gz" for _, _, run_id in uploads
    )
    sent = {archive for _, archive, _ in uploads}
    assert {path.read_bytes() for path in spooled} == sent  # the exact bytes each node sent
    assert list(scratch.iterdir()) == []  # every temporary upload file is gone
