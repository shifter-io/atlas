"""INGEST-HANG-1: an upload never parks in the orchestrator while imports are blocked.

26 Nov 2026: uploads waited for hours behind a stuck import's locks, each holding its
connection, its files (memory on the manager) and an executor thread, until the process ran out
of memory. Now an upload past `results.ingest_concurrency` in flight, or whose import waits for
a lock past UPLOAD_LOCK_WAIT, is answered 503 with Retry-After at once; its run is not failed,
and the node's retry is imported.
"""

from __future__ import annotations

import aiohttp
import pytest
from orchestrator_cases import planned_run, query, set_setting
from test_storage import measurement

from atlas import node_upload, storage
from atlas.orchestrator import api
from atlas.orchestrator.archive import pack
from atlas.storage import connect

pytest_plugins = ["test_storage", "orchestrator_cases"]


async def upload(database, db, client, tmp_path):
    batch_id, headers = await planned_run(database, db, client)
    directory = await measurement(tmp_path / "run", requests=3)
    body = pack(directory, tmp_path / "run.tar.gz").read_bytes()

    async def send():
        return await client.post(f"/v1/results?batch_id={batch_id}", data=body, headers=headers)

    return send


async def busy(reply):
    assert reply.status == 503, await reply.text()
    assert reply.headers["Retry-After"] == str(api.RETRY_AFTER)
    assert (await reply.json())["error"] == "ingest_busy"


def unfailed(database):
    return query(database, "SELECT status,error FROM batch_runs") == [("uploading", None)]


async def test_an_upload_past_the_process_s_concurrency_is_503_before_its_body(
    database, db, client, tmp_path
):
    send = await upload(database, db, client, tmp_path)
    await set_setting(db, "results.ingest_concurrency", 0)
    await busy(await send())
    assert api._uploads == [0] and unfailed(database)
    assert query(database, "SELECT count(*) FROM test_runs") == [(0,)]
    await set_setting(db, "results.ingest_concurrency", 2000)
    reply = await send()
    assert reply.status == 200, await reply.text()
    assert api._uploads == [0]


async def test_an_import_waiting_for_a_lock_past_the_upload_s_wait_is_503(
    database, db, client, tmp_path, monkeypatch
):
    """Another import holds the network's lock: the upload is answered after UPLOAD_LOCK_WAIT,
    not when the lock is freed; its retry once it is free is imported."""
    monkeypatch.setattr(api, "UPLOAD_LOCK_WAIT", "1s")
    send = await upload(database, db, client, tmp_path)
    with connect(database) as blocker:
        blocker.autocommit = True
        lock = (storage.NETWORK_LOCK, "fixture")
        blocker.execute("SELECT pg_advisory_lock(%s,hashtext(%s))", lock)
        await busy(await send())
    assert unfailed(database)
    reply = await send()
    assert reply.status == 200, await reply.text()
    assert (await reply.json())["imported"]


@pytest.mark.parametrize(
    ("headers", "seconds"),
    [({"Retry-After": "30"}, 30.0), ({"Retry-After": "86400"}, 300.0), ({"Retry-After": "x"}, 0)],
)
def test_a_node_waits_for_the_orchestrator_s_retry_after(headers, seconds):
    refused = aiohttp.ClientResponseError(None, (), status=503, headers=headers)
    assert node_upload.retry_after(refused) == seconds
    assert node_upload.retry_after(aiohttp.ClientConnectionError()) == 0
