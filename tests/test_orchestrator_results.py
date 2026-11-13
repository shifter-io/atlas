"""Orchestrator results: uploads, the raw spool, unsafe archives, outages and failed runs."""

import json
import tarfile
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from orchestrator_cases import archive_bytes, planned_run, product_id, query
from test_storage import measurement

from proxy_index.orchestrator import batches, nodework
from proxy_index.orchestrator.archive import ArchiveError, pack, unpack
from proxy_index.orchestrator.db import put_setting
from proxy_index.storage import connect

pytest_plugins = ["test_storage", "orchestrator_cases"]


# --- results --------------------------------------------------------------------------------


async def test_results_upload_ingests_and_links_the_run(database, db, client, tmp_path):
    batch_id, headers = await planned_run(database, db, client)
    directory = await measurement(tmp_path, requests=3)
    upload = pack(directory, tmp_path / "run.tar.gz").read_bytes()
    reply = await client.post(f"/v1/results?batch_id={batch_id}", data=upload, headers=headers)
    assert reply.status == 200, await reply.text()
    body = await reply.json()
    assert body["ingested"] and body["status"] == "done" and body["imported"]
    run = query(
        database,
        "SELECT r.status,r.run_key,r.requests,r.successes,r.unique_ips,r.stop_reason,t.batch_id "
        "FROM batch_runs r JOIN test_runs t USING(run_key)",
    )
    assert run == [("done", body["run_key"], 3, 3, 1, "requests", batch_id)]
    assert query(database, "SELECT status FROM test_batches") == [("done",)]
    ((product, signal, data),) = query(
        database, "SELECT product_id,signal,data FROM test_events WHERE kind='census_finished'"
    )
    assert product == product_id(database, "fixture", "residential") and signal == 0
    assert data["requests"] == 3 and data["unique_ips"] == 1 and data["region"] == "fra1"
    assert data["status"] == "done" and data["stop_reason"] == "requests"
    assert data["success_rate"] == 1.0 and data["duration_s"] >= 0
    # A retry after a lost response is acknowledged without a second import.
    retry = await client.post(f"/v1/results?batch_id={batch_id}", data=upload, headers=headers)
    assert (await retry.json())["run_key"] == body["run_key"]


async def test_an_ingested_upload_is_spooled_by_its_import_hour(database, db, client, tmp_path):
    spool = tmp_path / "spool"
    async with db.transaction() as conn:
        await put_setting(conn, "raw.spool_dir", str(spool))
    batch_id, headers = await planned_run(database, db, client)
    directory = await measurement(tmp_path, requests=3)
    upload = pack(directory, tmp_path / "run.tar.gz").read_bytes()
    reply = await client.post(f"/v1/results?batch_id={batch_id}", data=upload, headers=headers)
    assert reply.status == 200, await reply.text()
    run_id = json.loads((directory / "summary.json").read_text())["run_id"]
    (spooled,) = spool.glob("*/*/*/*/*.tar.gz")
    assert spooled.name == f"census-b{batch_id}-{run_id}.tar.gz"
    assert spooled.read_bytes() == upload and spooled.stat().st_mode & 0o777 == 0o600


async def test_a_spool_failure_after_the_ingest_still_finishes_the_run(
    database, db, client, tmp_path
):
    blocked = tmp_path / "blocked-spool"
    blocked.write_text("not a directory")  # the spool cannot create its hour folder
    async with db.transaction() as conn:
        await put_setting(conn, "raw.spool_dir", str(blocked))
    batch_id, headers = await planned_run(database, db, client)
    directory = await measurement(tmp_path, requests=3)
    upload = pack(directory, tmp_path / "run.tar.gz").read_bytes()
    reply = await client.post(f"/v1/results?batch_id={batch_id}", data=upload, headers=headers)
    assert reply.status == 200, await reply.text()
    body = await reply.json()
    assert body["ingested"] and body["status"] == "done"
    assert query(database, "SELECT status,run_key FROM batch_runs") == [("done", body["run_key"])]
    ((severity, data),) = query(
        database, "SELECT severity,data FROM alerts WHERE kind='raw_spool_failed'"
    )
    assert severity == "major" and data["batch_id"] == batch_id
    assert data["error"] in {"NotADirectoryError", "FileExistsError"}


@pytest.mark.parametrize(
    "member",
    [
        ("../evil.json", tarfile.REGTYPE, b"{}"),
        ("/etc/evil", tarfile.REGTYPE, b"{}"),
        ("sub/summary.json", tarfile.REGTYPE, b"{}"),
        ("summary.json", tarfile.SYMTYPE, None),
        ("summary.json", tarfile.LNKTYPE, None),
        ("folder", tarfile.DIRTYPE, None),
    ],
)
async def test_results_reject_unsafe_archives(database, db, client, tmp_path, member):
    batch_id, headers = await planned_run(database, db, client)
    name, kind, data = member
    info = tarfile.TarInfo(name)
    info.type = kind
    if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
        info.linkname = "/etc/passwd"
    if data is not None:
        info.size = len(data)
    reply = await client.post(
        f"/v1/results?batch_id={batch_id}", data=archive_bytes([(info, data)]), headers=headers
    )
    assert reply.status == 400
    assert not (tmp_path.parent / "evil.json").exists()
    assert query(database, "SELECT status,error LIKE 'bad archive:%%' FROM batch_runs") == [
        ("failed", True)
    ]
    assert query(database, "SELECT kind FROM alerts") == [("results_rejected",)]
    assert query(database, "SELECT status FROM test_batches") == [("failed",)]


def census_summary(started, *, successes, stalled_at=None):
    summary = {
        "status": "completed",
        "started_at": started.isoformat(),
        "completed": 500,
        "successes": successes,
        "unique_ips": successes,
        "stop_reason": "stalled" if stalled_at else "unique_ratio",
    }
    if stalled_at:
        summary["outage_started_at"] = stalled_at.isoformat()
    return summary


async def test_stalled_census_opens_an_outage_and_a_census_with_successes_closes_it(
    database, db, client
):
    batch_id, _ = await planned_run(database, db, client)
    ((node_id,),) = query(database, "SELECT node_id FROM nodes")
    node = {"node_id": node_id}
    alpha = product_id(database, "fixture", "residential")
    began = datetime.now(UTC).replace(microsecond=0)
    down_at = began + timedelta(minutes=5)
    summary = census_summary(began, successes=120, stalled_at=down_at)
    finished = began + timedelta(minutes=15)
    assert await nodework.finish_run(db, node, batch_id, summary, None, finished) == "done"
    # Replaying the upload changes nothing.
    assert await nodework.finish_run(db, node, batch_id, summary, None, finished) == "done"
    assert query(
        database,
        "SELECT o.product_id,o.country_code,r.slug,o.source,o.batch_id,o.started_at,o.ended_at "
        "FROM outages o JOIN runner_regions r USING(region_id)",
    ) == [(alpha, "DE", "fra1", "census", batch_id, down_at, None)]
    events = query(
        database,
        "SELECT kind,signal,severity,batch_id,country_code,data FROM test_events "
        "WHERE kind IN ('census_finished','down','recovered') ORDER BY event_id",
    )
    assert [(e[0], e[1]) for e in events] == [("census_finished", -1), ("down", -1)]
    assert events[0][5]["stop_reason"] == "stalled"
    kind, _, severity, event_batch, country, data = events[1]
    assert (severity, event_batch, country) == ("critical", batch_id, "DE")
    assert data == {
        "region": "fra1",
        "since": down_at.isoformat(),
        "source": "census",
        "batch_id": batch_id,
        "requests": 500,
        "successes": 120,
    }
    # The next day's census gets through: the outage ends when it started, once.
    tomorrow = finished + timedelta(days=1)
    with connect(database) as conn:
        conn.execute("UPDATE countries SET census_time=%s", (tomorrow.time(),))
    await batches.census_plan(db, tomorrow)
    ((second,),) = query(database, "SELECT max(batch_id) FROM test_batches")
    assert second != batch_id
    await nodework.assignments(db, node, tomorrow)
    restart = tomorrow + timedelta(minutes=1)
    healthy = census_summary(restart, successes=400)
    for _ in range(2):
        await nodework.finish_run(db, node, second, healthy, None, tomorrow + timedelta(hours=1))
    assert query(database, "SELECT started_at,ended_at FROM outages") == [(down_at, restart)]
    recovered = query(
        database,
        "SELECT signal,batch_id,data FROM test_events WHERE kind IN ('recovered','down') "
        "ORDER BY event_id",
    )[1:]
    assert len(recovered) == 1
    signal, event_batch, data = recovered[0]
    assert (signal, event_batch) == (1, second)
    assert data["source"] == "census" and data["since"] == down_at.isoformat()
    assert data["duration_s"] == (restart - down_at).total_seconds()
    assert data["outage_batch_id"] == batch_id


async def test_results_reject_foreign_and_invalid_runs(database, db, client, tmp_path):
    batch_id, headers = await planned_run(database, db, client, product="datacenter")
    directory = await measurement(tmp_path, requests=2)  # fixture/residential, not ours
    upload = pack(directory, tmp_path / "run.tar.gz").read_bytes()
    reply = await client.post(f"/v1/results?batch_id={batch_id}", data=upload, headers=headers)
    assert reply.status == 400
    assert "another provider or product" in (await reply.json())["error"]
    assert (await client.post("/v1/results?batch_id=x", data=b"", headers=headers)).status == 400


async def test_results_ingest_errors_fail_the_run(database, db, client, tmp_path):
    batch_id, headers = await planned_run(database, db, client)
    directory = await measurement(tmp_path, requests=2)
    summary = json.loads((directory / "summary.json").read_text())
    summary["successes"] += 1
    (directory / "summary.json").write_text(json.dumps(summary))
    upload = pack(directory, tmp_path / "run.tar.gz").read_bytes()
    reply = await client.post(f"/v1/results?batch_id={batch_id}", data=upload, headers=headers)
    assert reply.status == 422
    # The node gets the class only; the detail is in the run's error and the log.
    assert await reply.json() == {"error": "ingest_rejected", "reason": "invalid_data"}
    assert query(database, "SELECT status FROM batch_runs") == [("failed",)]
    assert query(database, "SELECT kind FROM alerts") == [("ingest_failed",)]
    conflict = await client.post(f"/v1/results?batch_id={batch_id}", data=upload, headers=headers)
    assert conflict.status == 409


async def test_a_database_refusal_reaches_the_node_without_its_text(
    database, db, client, tmp_path, monkeypatch
):
    """SEC-MANAGER-1: the database's error text never goes back to a node."""
    batch_id, headers = await planned_run(database, db, client)
    upload = pack(await measurement(tmp_path, requests=2), tmp_path / "run.tar.gz").read_bytes()
    detail = 'duplicate key value violates unique constraint "secret_index"'

    def refused(*_args, **_kwargs):
        raise psycopg.errors.UniqueViolation(detail)

    monkeypatch.setattr("proxy_index.orchestrator.api.storage.ingest", refused)
    reply = await client.post(f"/v1/results?batch_id={batch_id}", data=upload, headers=headers)
    assert reply.status == 422
    assert await reply.json() == {"error": "ingest_rejected", "reason": "database_refused"}
    assert query(database, "SELECT status,error FROM batch_runs") == [
        ("failed", f"ingest: {detail}")
    ]


async def test_failed_endpoint(database, db, client):
    batch_id, headers = await planned_run(database, db, client)
    reply = await client.post(
        "/v1/failed", json={"batch_id": batch_id, "error": "proxy down"}, headers=headers
    )
    assert await reply.json() == {"ok": True, "status": "failed"}
    assert query(database, "SELECT error FROM batch_runs") == [("proxy down",)]
    assert (
        await client.post("/v1/failed", json={"batch_id": batch_id}, headers=headers)
    ).status == 400
    assert (
        await client.post("/v1/failed", json={"batch_id": 424242, "error": "x"}, headers=headers)
    ).status == 404
    assert (await client.post("/v1/failed", data=b"[", headers=headers)).status == 400


def test_archive_limits(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "summary.json").write_text("{}" + " " * 1000)
    (tmp_path / "src" / "ips.jsonl.gz").write_bytes(b"x")
    archive = pack(tmp_path / "src", tmp_path / "a.tar.gz")
    (tmp_path / "out").mkdir()
    assert sorted(unpack(archive, tmp_path / "out", max_bytes=10_000)) == [
        "ips.jsonl.gz",
        "summary.json",
    ]
    (tmp_path / "small").mkdir()
    with pytest.raises(ArchiveError, match="extraction limit"):
        unpack(archive, tmp_path / "small", max_bytes=100)
    (tmp_path / "junk.tar.gz").write_bytes(b"not a tarball")
    (tmp_path / "junk").mkdir()
    with pytest.raises(ArchiveError, match="Unreadable"):
        unpack(tmp_path / "junk.tar.gz", tmp_path / "junk", max_bytes=100)
    info = tarfile.TarInfo("ips.jsonl.gz")
    info.size = 1
    (tmp_path / "nosummary.tar.gz").write_bytes(archive_bytes([(info, b"x")]))
    (tmp_path / "ns").mkdir()
    with pytest.raises(ArchiveError, match="no summary"):
        unpack(tmp_path / "nosummary.tar.gz", tmp_path / "ns", max_bytes=100)
    link = tarfile.TarInfo("summary.json")
    link.type, link.linkname = tarfile.SYMTYPE, "../outside"
    (tmp_path / "link.tar.gz").write_bytes(archive_bytes([(link, None)]))
    (tmp_path / "ln").mkdir()
    with pytest.raises(ArchiveError, match="only regular files"):
        unpack(tmp_path / "link.tar.gz", tmp_path / "ln", max_bytes=100)
    assert not list((tmp_path / "ln").iterdir())
