"""Browser bundle ingest (K-BP7), concurrency and run states: uploads at once, the ingest
semaphore, duplicates in flight, archive and database failures, late and stopped runs."""

import asyncio

import pytest
from browser_ingest_cases import (
    HOSTS,
    KEY,
    Gate,
    assert_nothing_kept,
    browser_run,
    make_bundle,
    run_rows,
    set_settings,
    sha256,
    upload,
)
from orchestrator_cases import query

from atlas.archive import ArchiveError
from atlas.archive.spaces import Spaces
from atlas.browser import contracts as bc
from atlas.orchestrator import browser_ingest
from atlas.storage import connect

# The fixtures (db, client, scratch, world) are browser_ingest_cases'.
pytest_plugins = ["test_storage", "browser_ingest_cases"]


# --- concurrency ------------------------------------------------------------------------------


async def test_three_minis_upload_twelve_bundles_at_once(database, db, client, world, scratch):
    await set_settings(db, {"browser.ingest_concurrency": 12})
    targets = bc.PAGE_TARGETS[:4]
    uploads = []
    for provider in HOSTS:
        for target in targets:
            batch_id = browser_run(database, provider, target=target)
            data, run_id = await make_bundle(world["tmp"], batch_id, provider, target=target)
            uploads.append((world["tokens"][provider], batch_id, data, run_id))
    replies = await asyncio.gather(*(upload(client, *args) for args in uploads))
    assert [status for status, _, _ in replies] == [200] * 12, replies
    assert len(world["bucket"].objects) == 12
    assert query(database, "SELECT count(*) FROM test_runs") == [(12,)]
    assert query(database, "SELECT count(*) FROM batch_runs WHERE status='done'") == [(12,)]
    assert_nothing_kept(world, scratch)


async def test_a_full_ingest_semaphore_answers_503_before_the_body_and_retries_succeed(
    database, db, client, world, scratch, monkeypatch
):
    await set_settings(db, {"browser.ingest_concurrency": 2})
    uploads = []
    for provider in ("alpha", "beta", "gamma"):
        batch_id = browser_run(database, provider)
        data, run_id = await make_bundle(world["tmp"], batch_id, provider)
        uploads.append((world["tokens"][provider], batch_id, data, run_id))
    gate = Gate(monkeypatch, hold=2)
    first = [asyncio.create_task(upload(client, *args)) for args in uploads[:2]]
    await gate.until_held()
    receive = browser_ingest._receive

    async def unread(*_args):
        raise AssertionError("a full manager must not read the body")

    monkeypatch.setattr(browser_ingest, "_receive", unread)
    status, body, headers = await upload(client, *uploads[2])
    assert (status, body) == (503, {"error": "ingest_busy"}) and headers["Retry-After"] == "30"
    monkeypatch.setattr(browser_ingest, "_receive", receive)
    gate.release.set()
    assert [reply[0] for reply in await asyncio.gather(*first)] == [200, 200]
    assert (await upload(client, *uploads[2]))[0] == 200
    assert len(world["bucket"].objects) == 3
    assert_nothing_kept(world, scratch)


async def test_a_duplicate_in_flight_gets_429_and_a_replay_adds_nothing(
    database, client, world, scratch, monkeypatch
):
    batch_id = browser_run(database, "alpha")
    args = (world["tokens"]["alpha"], batch_id, *await make_bundle(world["tmp"], batch_id))
    gate = Gate(monkeypatch, hold=1)
    first = asyncio.create_task(upload(client, *args))
    await gate.until_held()
    status, body, headers = await upload(client, *args)
    assert status == 429 and headers["Retry-After"] == "10", body
    gate.release.set()
    status, body, _ = await first
    assert status == 200 and body["status"] == "done"
    replay_status, replay, _ = await upload(client, *args)
    assert replay_status == 200 and replay == body
    assert query(database, "SELECT count(*) FROM test_runs") == [(1,)]
    assert len(world["bucket"].objects) == 1
    assert_nothing_kept(world, scratch)


# --- run states, late data, failures ----------------------------------------------------------


async def test_a_spaces_failure_answers_503_and_a_retry_succeeds(
    database, client, world, scratch, monkeypatch
):
    batch_id = browser_run(database, "alpha")
    args = (world["tokens"]["alpha"], batch_id, *await make_bundle(world["tmp"], batch_id))
    put_file = Spaces.put_file
    calls = []

    def failing_once(self, *a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise ArchiveError("Spaces PUT failed: ConnectionResetError")
        return put_file(self, *a, **k)

    monkeypatch.setattr(Spaces, "put_file", failing_once)
    status, body, _ = await upload(client, *args)
    assert status == 503 and "archive unavailable" in body["error"]
    assert query(database, "SELECT * FROM test_runs") == [] and world["bucket"].objects == {}
    assert run_rows(database, batch_id)[0][:2] == ("uploading", None)
    assert_nothing_kept(world, scratch)
    status, body, _ = await upload(client, *args)
    assert status == 200 and body["status"] == "done"


async def test_a_database_failure_after_the_archive_is_retried_idempotently(
    database, client, world, scratch, monkeypatch
):
    import psycopg

    from atlas.orchestrator import nodework

    batch_id = browser_run(database, "alpha")
    args = (world["tokens"]["alpha"], batch_id, *await make_bundle(world["tmp"], batch_id))
    finish = nodework.finish_run

    async def unreachable(*a, **k):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(nodework, "finish_run", unreachable)
    status, body, _ = await upload(client, *args)
    assert (status, body) == (503, {"error": "database unavailable; retry"})
    assert len(world["bucket"].objects) == 1
    monkeypatch.setattr(nodework, "finish_run", finish)
    status, body, _ = await upload(client, *args)
    assert status == 200 and body["status"] == "done"
    assert query(database, "SELECT count(*) FROM test_runs") == [(1,)]
    assert len(world["bucket"].objects) == 1


async def test_a_late_upload_for_a_reaped_run_is_ingested_and_closes_the_batch_done(
    database, client, world, scratch
):
    batch_id = browser_run(database, "alpha")
    with connect(database) as conn:  # what the reaper leaves: run failed 'stale', batch failed
        conn.execute(
            "UPDATE batch_runs SET status='failed',error='stale',stop_reason='error',"
            "finished_at=now() WHERE batch_id=%s",
            (batch_id,),
        )
        conn.execute(
            "UPDATE test_batches SET status='failed',finished_at=now() WHERE batch_id=%s",
            (batch_id,),
        )
    data, run_id = await make_bundle(world["tmp"], batch_id)
    status, body, _ = await upload(client, world["tokens"]["alpha"], batch_id, data, run_id)
    assert status == 200 and body["status"] == "done", body
    assert query(
        database, "SELECT status,error,run_key FROM batch_runs WHERE batch_id=%s", (batch_id,)
    ) == [("done", None, body["run_key"])]
    assert query(database, "SELECT status FROM test_batches WHERE batch_id=%s", (batch_id,)) == [
        ("done",)
    ]


@pytest.mark.parametrize("state", ["killed", "skipped"])
async def test_a_stopped_run_is_discarded_with_ingested_false(
    database, client, world, scratch, state
):
    batch_id = browser_run(database, "alpha", status=state)
    data, run_id = await make_bundle(world["tmp"], batch_id)
    status, body, _ = await upload(client, world["tokens"]["alpha"], batch_id, data, run_id)
    assert (status, body) == (200, {"ok": True, "ingested": False, "status": state})
    assert bc.confirmed((status, body), sha256(data))
    assert world["bucket"].objects == {} and query(database, "SELECT * FROM test_runs") == []
    assert_nothing_kept(world, scratch)


async def test_a_queued_or_refused_run_is_a_conflict(database, client, world, scratch):
    batch_id = browser_run(database, "alpha", status="queued")
    data, run_id = await make_bundle(world["tmp"], batch_id)
    status, body, _ = await upload(client, world["tokens"]["alpha"], batch_id, data, run_id)
    assert (status, body) == (409, {"error": "run is queued"})


async def test_an_archived_object_with_other_bytes_is_never_overwritten(
    database, client, world, scratch
):
    batch_id = browser_run(database, "alpha")
    data, run_id = await make_bundle(world["tmp"], batch_id)
    meta = [("x-amz-meta-sha256", "b" * 64)]
    world["bucket"].objects["/" + KEY] = (b"older bytes", meta, "private")
    status, body, _ = await upload(client, world["tokens"]["alpha"], batch_id, data, run_id)
    assert (status, body) == (409, {"error": bc.ERROR_BUNDLE_CHANGED})
    assert world["bucket"].objects["/" + KEY][0] == b"older bytes"
    ((severity, data_),) = query(
        database, "SELECT severity,data FROM alerts WHERE kind=%s", (bc.ALERT_BUNDLE_CHANGED,)
    )
    assert severity == "major" and data_["key"] == KEY and data_["sha256"] == sha256(data)
    assert query(database, "SELECT * FROM test_runs") == []
    assert_nothing_kept(world, scratch)


async def test_a_replay_checks_the_archived_object(database, client, world, scratch):
    batch_id = browser_run(database, "alpha")
    args = (world["tokens"]["alpha"], batch_id, *await make_bundle(world["tmp"], batch_id))
    assert (await upload(client, *args))[0] == 200
    world["bucket"].objects["/" + KEY] = (b"x", [("x-amz-meta-sha256", "c" * 64)], "private")
    assert (await upload(client, *args))[:2] == (409, {"error": bc.ERROR_BUNDLE_CHANGED})
    del world["bucket"].objects["/" + KEY]
    status, body, _ = await upload(client, *args)
    assert status == 409 and "not archived" in body["error"]
