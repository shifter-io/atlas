"""ORCH-MEM-1: the orchestrator's memory gauge (orchestrator/memory.py): the process's resident
memory and the sizes of what it keeps, in the job `memory`, its alert and the admin's health."""

import logging
from datetime import UTC, datetime

from admin_cases import get, serve
from dbip_fakes import DE, US, tiny_mmdb
from orchestrator_cases import bearer, prepare, query, set_setting
from test_storage import enrichment, measurement

from atlas.enrichment import MMDB_ENV
from atlas.orchestrator import batches, memory, nodework
from atlas.orchestrator.app import SCHEDULER_KEY, build_app
from atlas.orchestrator.archive import pack
from atlas.orchestrator.memory import GAUGE_KEY, KIND, MemoryGauge, resident
from atlas.orchestrator.nodes import issue_token

pytest_plugins = ["test_storage", "admin_cases"]

UPLOADS = 12
STATUS = "Name:\tpython\nVmHWM:\t  204800 kB\nVmRSS:\t  102400 kB\nRssAnon:\t   81920 kB\n"
STATUS += "RssFile:\t   20480 kB\nRssShmem:\t       0 kB\nThreads:\t9\n"


def test_resident_reads_proc_status_in_mb(tmp_path):
    path = tmp_path / "status"
    path.write_text(STATUS)
    assert resident(path) == {"peak_mb": 200, "rss_mb": 100, "anon_mb": 80, "file_mb": 20}


def test_without_proc_status_only_the_peak_is_measured(tmp_path):
    found = resident(tmp_path / "missing")
    assert list(found) == ["peak_mb"] and found["peak_mb"] > 0


def test_measure_sums_the_sources_and_names_one_that_fails(monkeypatch):
    monkeypatch.setattr(memory, "resident", lambda: {"rss_mb": 100})

    def broken():
        raise KeyError("gone")

    found = MemoryGauge([lambda: {"b": 2}, broken, lambda: {"a": 1}]).measure()
    assert found["rss_mb"] == 100 and found["threads"] >= 1 and found["python_blocks"] > 0
    name = broken.__qualname__
    assert found["structures"] == {"a": 1, "b": 2, name: "unreadable (KeyError)"}
    assert list(found["structures"]) == sorted(found["structures"])


def alerts(database):
    return query(
        database, "SELECT kind,severity,occurrences,acknowledged_at IS NOT NULL FROM alerts"
    )


async def test_the_job_alerts_at_the_limit_and_acknowledges_below_it(
    database, db, monkeypatch, caplog
):
    rss = {"rss_mb": 5000}
    monkeypatch.setattr(memory, "resident", lambda: dict(rss))
    gauge = MemoryGauge([lambda: {"uploads_in_flight": 3}])
    with caplog.at_level(logging.INFO, logger=memory.__name__):
        found = await gauge.check(db)  # the default limit, 4096 MB
    assert found["structures"] == {"uploads_in_flight": 3}
    assert "rss_mb=5000" in caplog.text and "uploads_in_flight=3" in caplog.text
    assert alerts(database) == [(KIND, "major", 1, False)]
    ((data,),) = query(database, "SELECT data FROM alerts")
    assert data["limit_mb"] == 4096 and data["rss_mb"] == 5000
    await gauge.check(db)
    assert alerts(database) == [(KIND, "major", 2, False)]
    rss["rss_mb"] = 300
    await gauge.check(db)
    assert alerts(database) == [(KIND, "major", 2, True)]


async def test_without_a_limit_or_a_resident_size_nothing_is_alerted(database, db, monkeypatch):
    monkeypatch.setattr(memory, "resident", lambda: {"rss_mb": 99_999})
    await set_setting(db, "orchestrator.memory_alert_mb", None)
    await MemoryGauge().check(db)
    monkeypatch.setattr(memory, "resident", lambda: {"peak_mb": 99_999})  # off Linux
    await set_setting(db, "orchestrator.memory_alert_mb", 1)
    await MemoryGauge().check(db)
    assert alerts(database) == []


async def test_the_app_gauges_its_structures_every_five_minutes(admin):
    app = admin.server.app
    scheduler = app[SCHEDULER_KEY]
    assert scheduler.tasks["memory"].every_s == 300
    structures = app[GAUGE_KEY].measure()["structures"]
    for name in ("db_idle_connections", "db_close_hooks", "jobs_fired", "uploads_in_flight"):
        assert structures[name] >= 0
    assert structures["uploads_in_flight"] == 0 and structures["browser_bundles_in_flight"] == 0
    health = (await get(admin, "/health"))["memory"]
    assert health["structures"]["jobs"] == len(scheduler.tasks)
    stats = await scheduler.run_now("memory")
    assert stats["structures"]["jobs"] == len(scheduler.tasks)


def test_with_a_db_ip_file_its_cached_lookups_are_gauged(database, tmp_path, monkeypatch):
    monkeypatch.setenv(MMDB_ENV, str(tmp_path / "dbip.mmdb"))  # opened on the first pulse
    app = build_app(database, with_scheduler=False, admin_credential=None)
    assert app[GAUGE_KEY].measure()["structures"]["dbip_lookups_cached"] == 0


async def test_after_many_uploads_the_process_keeps_only_bounded_structures(
    database, db, tmp_path, monkeypatch
):
    """ORCH-MEM-1's review in a test: census and pulse uploads, the jobs and the close hooks
    leave nothing behind but the pool's idle connections and the DB-IP cache under its cap."""
    mmdb = tmp_path / "dbip.mmdb"
    mmdb.write_bytes(tiny_mmdb(US, DE))
    monkeypatch.setenv(MMDB_ENV, str(mmdb))
    prepare(database, ("DE",))
    node = issue_token(database, provider="fixture", product="residential", region="fra1")
    headers = bearer(node["token"])
    client = await serve(build_app(database, with_scheduler=False, db=db, admin_credential=None))
    try:
        for i in range(UPLOADS):
            await upload_one(database, db, client, headers, tmp_path / str(i), pulse=i % 2 == 1)
            await nodework.reap(db)
            await db.drain()
        stats = await client.server.app[SCHEDULER_KEY].run_now("memory")
    finally:
        await client.close()
    structures = stats["structures"]
    assert query(database, "SELECT count(*) FROM test_runs") == [(UPLOADS,)]
    assert structures["uploads_in_flight"] == structures["results_watch_open"] == 0
    assert structures["db_close_hooks"] == structures["db_close_marks"] == 0
    # Fired by hand, the memory job runs while it measures; no fire of the clock is left.
    assert (structures["jobs_fired"], structures["jobs_running"]) == (0, 1)
    assert structures["db_idle_connections"] <= 8 and structures["db_partitions_known"] == 1
    assert 0 < structures["dbip_lookups_cached"] <= 100_000  # LocalEnricher's cache_size


async def upload_one(database, db, client, headers, directory, *, pulse):
    now = datetime.now(UTC)
    query(database, "UPDATE countries SET census_time=%s RETURNING 1", (now.time(),))
    await batches.census_plan(db, now)
    ((batch_id,),) = query(database, "SELECT max(batch_id) FROM test_batches")
    if pulse:
        pulse_sql = "UPDATE test_batches SET kind='pulse' WHERE batch_id=%s RETURNING 1"
        query(database, pulse_sql, (batch_id,))
    await client.get("/v1/work", headers=headers)
    run = await measurement(directory, requests=3)
    if pulse:
        enrichment(run, release="b" * 64)
    body = pack(run, directory / "run.tar.gz").read_bytes()
    reply = await client.post(f"/v1/results?batch_id={batch_id}", data=body, headers=headers)
    assert reply.status == 200, await reply.text()
