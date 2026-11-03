"""Readers follow `dbip update` (DBIP-DAILY-1): the `current` link, LocalEnricher.reload, the
node's reopen between runs and the orchestrator's daily `dbip_update` job."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import maxminddb
import pytest
from dbip_fakes import DE, US, FakeCdn, tiny_mmdb

from proxy_index import dbip
from proxy_index.enrichment import LocalEnricher, find_database, find_source
from proxy_index.node import NodeAgent
from proxy_index.orchestrator.db import Database, put_setting
from proxy_index.orchestrator.jobs import dbip_update, register_jobs
from proxy_index.orchestrator.scheduler import Scheduler

pytest_plugins = ["test_storage"]


def releases(root: Path) -> Path:
    """db-ip/ with release a (low: US) behind `current` and release b (low: DE) beside it."""
    root.mkdir()
    (root / "a.mmdb").write_bytes(tiny_mmdb(low=US, marker="a"))
    (root / "b.mmdb").write_bytes(tiny_mmdb(low=DE, marker="b"))
    (root / "current").symlink_to("a.mmdb")
    return root


def swing(root: Path, name: str) -> None:
    (root / "next").symlink_to(name)
    (root / "next").replace(root / "current")


def test_discovery_prefers_the_current_link(tmp_path, monkeypatch):
    monkeypatch.delenv("DBIP_MMDB", raising=False)
    monkeypatch.chdir(tmp_path)
    root = releases(tmp_path / "db-ip")  # two *.mmdb files: only the link makes it unambiguous
    assert find_source() == root / "current"
    assert find_database() == (root / "a.mmdb").resolve()
    monkeypatch.setenv("DBIP_MMDB", str(root / "current"))
    assert find_source() == root / "current"


def test_the_enricher_reopens_when_current_moves(tmp_path):
    root = releases(tmp_path / "db-ip")
    with LocalEnricher(root / "current") as enricher:
        assert enricher.lookup("127.0.0.1")["country_code"] == "US"
        first = enricher.release
        assert not enricher.stale() and enricher.reload() is False
        swing(root, "b.mmdb")
        assert enricher.stale() and enricher.reload() is True
        assert enricher.path == (root / "b.mmdb").resolve() and enricher.release["file"] == "b.mmdb"
        assert enricher.release["sha256"] != first["sha256"]
        assert enricher.lookup("127.0.0.1")["country_code"] == "DE"  # the cache was cleared
        (root / "broken.mmdb").write_bytes(b"not a database")
        swing(root, "broken.mmdb")
        with pytest.raises(maxminddb.InvalidDatabaseError):
            enricher.reload()
        assert enricher.lookup("198.51.100.7")["country_code"] == "DE"  # b is still open
        (root / "current").unlink()
        assert enricher.stale() is False  # mid-swap: keep the open release


def agent(tmp_path, enricher) -> NodeAgent:
    return NodeAgent(
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


async def test_a_node_reopens_the_database_only_between_runs(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="proxy_index.node")
    root = releases(tmp_path / "db-ip")
    with LocalEnricher(root / "current") as enricher:
        node = agent(tmp_path, enricher)
        assert node.mmdb == root / "current"  # finished runs are enriched with the newest
        swing(root, "b.mmdb")
        node.active[1] = object()
        await node.reopen_database()
        assert enricher.path.name == "a.mmdb"  # a run is active: it keeps its release
        node.active.clear()
        await node.reopen_database()
        assert enricher.path.name == "b.mmdb" and "reopened: b.mmdb" in caplog.text
        (root / "broken.mmdb").write_bytes(b"x")
        swing(root, "broken.mmdb")
        await node.reopen_database()
        assert enricher.path.name == "b.mmdb" and "could not reopen" in caplog.text
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
        assert done["dbip"] == "updated" and done["removed"] == []
        assert (tmp_path / "db-ip" / "current").resolve().name == done["file"]
        again = await dbip_update(db, str(database))
        assert again == {"skipped": "release unchanged", "file": done["file"]}
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
