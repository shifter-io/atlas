"""Readers follow `dbip update` (DBIP-DAILY-1): discovery of db-ip/dbip-full.mmdb,
LocalEnricher.reload after the file is replaced, the node's reopen between runs and the
orchestrator's daily `dbip_update` job."""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import maxminddb
import pytest
from dbip_fakes import DE, US, FakeCdn, tiny_mmdb

from atlas import dbip
from atlas.enrichment import LocalEnricher, find_database, find_source
from atlas.node import NodeAgent, NodeSettings
from atlas.orchestrator.db import Database, put_setting
from atlas.orchestrator.jobs import dbip_update, register_jobs
from atlas.orchestrator.scheduler import Scheduler

pytest_plugins = ["test_storage"]
NAME = "dbip-full.mmdb"


def releases(root: Path) -> Path:
    """db-ip/ with release a (low half: US) under the fixed name."""
    root.mkdir()
    (root / NAME).write_bytes(tiny_mmdb(low=US, marker="a"))
    return root


def replace(root: Path, data: bytes) -> None:
    """What `dbip update` does: a new file renamed over the fixed name."""
    (root / ".next").write_bytes(data)
    (root / ".next").replace(root / NAME)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_discovery_prefers_the_fixed_name(tmp_path, monkeypatch):
    monkeypatch.delenv("DBIP_MMDB", raising=False)
    monkeypatch.chdir(tmp_path)
    root = releases(tmp_path / "db-ip")
    (root / "dbip-full-2026-09-14.mmdb").write_bytes(b"an older manual copy")  # two *.mmdb
    assert find_source() == root / NAME and find_database() == (root / NAME).resolve()
    monkeypatch.setenv("DBIP_MMDB", str(root / NAME))
    assert find_source() == root / NAME


def test_the_enricher_reopens_the_replaced_file(tmp_path):
    root = releases(tmp_path / "db-ip")
    with LocalEnricher(root / NAME) as enricher:
        assert enricher.lookup("127.0.0.1")["country_code"] == "US"
        first = enricher.release
        assert not enricher.stale() and enricher.reload() is False
        replace(root, tiny_mmdb(low=DE, marker="b"))
        assert enricher.stale() and enricher.reload() is True
        assert enricher.release["file"] == NAME and enricher.release["sha256"] == sha(root / NAME)
        assert enricher.release["sha256"] != first["sha256"]
        assert enricher.lookup("127.0.0.1")["country_code"] == "DE"  # the cache was cleared
        replace(root, b"not a database")
        with pytest.raises(maxminddb.InvalidDatabaseError):
            enricher.reload()
        assert enricher.lookup("198.51.100.7")["country_code"] == "DE"  # b is still open
        (root / NAME).unlink()
        assert enricher.stale() is False  # gone: keep the open release


def test_a_file_replaced_while_it_is_opened_is_opened_again(tmp_path, monkeypatch):
    root = releases(tmp_path / "db-ip")
    real, opened = maxminddb.open_database, []

    def racing(path):
        reader = real(path)
        opened.append(path)
        if len(opened) == 1:  # the update renames a new release over it mid-open
            replace(root, tiny_mmdb(low=DE, marker="b"))
        return reader

    monkeypatch.setattr("atlas.enrichment.maxminddb.open_database", racing)
    with LocalEnricher(root / NAME) as enricher:
        assert len(opened) == 2 and enricher.release["sha256"] == sha(root / NAME)
        assert enricher.lookup("127.0.0.1")["country_code"] == "DE"

    def always(path):
        reader = real(path)
        opened.append(path)
        replace(root, tiny_mmdb(marker=chr(ord("c") + len(opened))))
        return reader

    monkeypatch.setattr("atlas.enrichment.maxminddb.open_database", always)
    with pytest.raises(OSError, match="kept changing"):
        LocalEnricher(root / NAME)


def agent(tmp_path, enricher) -> NodeAgent:
    return NodeAgent(
        NodeSettings(
            "http://orchestrator.invalid",
            "t",
            provider="fixture",
            product="alpha",
            region="fra1",
            proxy_factory=lambda country: None,
            work_dir=tmp_path / "runs",
            enricher=enricher,
            mmdb=getattr(enricher, "source", None),
        )
    )


async def test_a_node_reopens_the_database_only_between_runs(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="atlas.node")
    root = releases(tmp_path / "db-ip")
    with LocalEnricher(root / NAME) as enricher:
        node = agent(tmp_path, enricher)
        first = enricher.release["sha256"]
        assert node.mmdb == root / NAME  # finished runs are enriched with the file's release
        replace(root, tiny_mmdb(low=DE, marker="b"))
        node.active[1] = object()
        await node.reopen_database()
        assert enricher.release["sha256"] == first  # a run is active: it keeps its release
        node.active.clear()
        await node.reopen_database()
        second = sha(root / NAME)
        assert enricher.release["sha256"] == second and f"reopened: {second[:12]}" in caplog.text
        replace(root, b"x")
        await node.reopen_database()
        assert enricher.release["sha256"] == second and "could not reopen" in caplog.text
    await agent(tmp_path, None).reopen_database()  # no enricher: nothing to do


@pytest.fixture
async def db(database):
    pool = Database(database)
    yield pool
    await pool.close()


async def test_the_daily_job_updates_records_and_skips(tmp_path, database, db, monkeypatch):
    monkeypatch.setattr(dbip, "DEFAULT_CHECKS", dbip.Checks(min_bytes=1))
    scheduler = Scheduler(db)
    register_jobs(scheduler, db, str(database))
    assert scheduler.tasks["dbip_update"].at_utc_minute == 5 * 60 + 23
    assert await dbip_update(db, str(database)) == {"skipped": "dbip.dir is not set"}

    cdn = FakeCdn()
    try:
        today = datetime.now(UTC).date()
        cdn.files[f"dbip-full-{today - timedelta(days=3)}.mmdb"] = tiny_mmdb()
        async with db.connection() as conn:
            await put_setting(conn, "dbip.dir", str(tmp_path / "db-ip"))
            await put_setting(conn, "dbip.base_url", cdn.url)
        done = await scheduler.run_now("dbip_update")
        name = f"dbip-full-{today - timedelta(days=3)}.mmdb"
        assert done["dbip"] == "updated" and done["release"] == name
        assert sha(tmp_path / "db-ip" / NAME) == done["sha256"]
        again = await dbip_update(db, str(database))
        assert again == {"skipped": "release unchanged", "release": name}
        cdn.status[f"dbip-full-{today}.mmdb"] = 503
        assert await scheduler.run_now("dbip_update") is None  # failed, recorded and alerted
        async with db.connection() as conn:
            rows = await (
                await conn.execute("SELECT status,error FROM job_runs WHERE job='dbip_update'")
            ).fetchall()
            alerts = await (await conn.execute("SELECT kind FROM alerts")).fetchall()
            recorded = await (
                await conn.execute("SELECT count(*) AS n FROM dbip_releases")
            ).fetchone()
        assert [r["status"] for r in rows] == ["ok", "failed"]
        assert rows[1]["error"] == f"UpdateError: probe dbip-full-{today}.mmdb: HTTP 503"
        assert recorded["n"] == 1 and [a["kind"] for a in alerts] == ["job_failed"]
    finally:
        cdn.close()


async def test_the_job_reports_a_busy_directory_and_a_recording_failure(
    tmp_path, database, db, monkeypatch
):
    def busy(dest, base, **options):
        raise dbip.Busy("another update is running in this directory")

    async with db.connection() as conn:
        await put_setting(conn, "dbip.dir", str(tmp_path / "db-ip"))
    monkeypatch.setenv("DBIP_BASE_URL", "https://cdn.invalid/dbip")
    monkeypatch.setattr(dbip, "update", busy)
    assert await dbip_update(db, str(database)) == {"skipped": "another update is running"}
    monkeypatch.setattr(dbip, "update", lambda dest, base, **o: {"dbip": "updated", "error": "x"})
    with pytest.raises(dbip.UpdateError, match="x"):
        await dbip_update(db, str(database))
