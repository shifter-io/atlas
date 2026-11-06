"""Orchestrator jobs and the scheduler: runs, schedules, calculate, detect, retention, the CLI."""

import asyncio
import json
from datetime import UTC, datetime, time, timedelta

import pytest
from orchestrator_cases import NOW, prepare, query, set_setting

from proxy_index.orchestrator import batches, jobs, nodework
from proxy_index.orchestrator.jobs import calculate, detect, retention
from proxy_index.orchestrator.nodes import issue_token
from proxy_index.orchestrator.scheduler import Scheduler, Task
from proxy_index.storage import connect

pytest_plugins = ["test_storage", "orchestrator_cases"]


# --- jobs and scheduler ---------------------------------------------------------------------


async def test_scheduler_records_runs_failures_and_skips(database, db):
    scheduler = Scheduler(db)
    calls = []

    async def ok():
        calls.append("ok")
        return {"n": 1}

    async def broken():
        raise RuntimeError("nope")

    async def idle():
        return {"skipped": "nothing to do"}

    scheduler.add("ok", ok, every_s=60)
    scheduler.add("broken", broken, every_s=60)
    scheduler.add("idle", idle, every_s=60)
    assert await scheduler.run_now("ok") == {"n": 1}
    assert await scheduler.run_now("broken") is None
    assert await scheduler.run_now("broken") is None
    await scheduler.run_now("idle")
    scheduler.tasks["ok"].running = True
    assert await scheduler.run_now("ok") is None
    with pytest.raises(KeyError):
        await scheduler.run_now("missing")
    rows = query(database, "SELECT job,status,error FROM job_runs ORDER BY id")
    assert rows == [
        ("ok", "ok", None),
        ("broken", "failed", "RuntimeError: nope"),
        ("broken", "failed", "RuntimeError: nope"),
        ("idle", "skipped", "nothing to do"),
        ("ok", "skipped", "still running"),
    ]
    assert query(database, "SELECT kind,dedupe_key,occurrences FROM alerts") == [
        ("job_failed", "job_failed:broken", 2)
    ]
    assert [t["job"] for t in scheduler.upcoming()] == ["broken", "idle", "ok"]


def test_task_schedules():
    at = datetime(2026, 10, 1, 12, 0, 30, tzinfo=UTC)

    async def job():
        return None

    assert Task("a", job, every_s=60, aligned=True).schedule(at) == datetime(
        2026, 10, 1, 12, 1, tzinfo=UTC
    )
    assert Task("b", job, every_s=15).schedule(at) == at + timedelta(seconds=15)
    assert Task("c", job, at_minute=0).schedule(at) == datetime(2026, 10, 1, 13, tzinfo=UTC)
    assert Task("d", job, at_utc_minute=30).schedule(at) == datetime(2026, 10, 2, 0, 30, tzinfo=UTC)


async def test_scheduler_loop_fires_due_jobs(db):
    scheduler = Scheduler(db)
    fired = []

    async def job():
        fired.append(1)

    scheduler.add("fast", job, every_s=0.05)
    scheduler.start()
    for _ in range(100):
        if len(fired) >= 2:
            break
        await asyncio.sleep(0.05)
    await scheduler.stop()
    assert len(fired) >= 2


async def test_calculate_is_debounced_until_a_batch_finishes(database, db, monkeypatch):
    calls = []

    async def fake(argv, limit_s):
        calls.append(argv[-2:])
        return {"snapshot_id": "s1", "as_of": "now"}

    # A stand-in for the subprocess (test_manager_orch runs the real one).
    monkeypatch.setattr(jobs, "run_calculation", fake)
    await set_setting(db, "calculate.trigger", "batch")  # the old schedule; hourly is the default
    assert (await calculate(db, database, "m.toml"))["skipped"].startswith("no batch")
    prepare(database, ("DE",), DE=time(12))
    issue_token(database, provider="alpha", product="res", region="fra1")
    await batches.census_plan(db, NOW)
    with connect(database) as conn:
        conn.execute("UPDATE test_batches SET status='done',finished_at=now()")
    assert await calculate(db, database, "m.toml") == {"snapshot_id": "s1", "as_of": "now"}
    with connect(database) as conn:
        conn.execute("INSERT INTO job_runs(job,status,started_at) VALUES ('calculate','ok',now())")
        conn.execute("UPDATE test_batches SET finished_at=now()+interval '1 second'")
    assert (await calculate(db, database, "m.toml")) == {"skipped": "debounced"}
    assert calls == [["--publish", "--incremental"]]


async def test_detect_job_is_optional(database, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "proxy_index.detect", None)
    assert (await detect(database))["skipped"].startswith("proxy_index.detect")

    class Module:
        @staticmethod
        def detect(conn, now):
            assert now.tzinfo is UTC
            return 3

    monkeypatch.setitem(sys.modules, "proxy_index.detect", Module)
    assert await detect(database) == {"events": 3}


async def test_retention_drops_old_partitions_and_job_runs(database, db):
    old = datetime(2026, 1, 15, tzinfo=UTC)
    recent = datetime(2026, 9, 20, tzinfo=UTC)
    for moment in (old, recent):
        await db.ensure_partition("test_events", moment)
    with connect(database) as conn:
        for moment in (old, recent, datetime(2026, 7, 1, tzinfo=UTC)):
            if moment.month == 7:
                conn.execute(
                    "CREATE TABLE test_events_2026_07 PARTITION OF test_events "
                    "FOR VALUES FROM ('2026-07-01') TO ('2026-08-01')"
                )
            conn.execute("INSERT INTO test_events(at,kind) VALUES (%s,'x')", (moment,))
        conn.execute(
            "INSERT INTO job_runs(job,status,started_at) VALUES ('a','ok',%s),('b','ok',%s)",
            (datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 9, 30, tzinfo=UTC)),
        )
    result = await retention(db, datetime(2026, 10, 1, tzinfo=UTC))
    assert result["dropped_partitions"] == ["test_events_2026_01"]
    assert result["events_deleted"] == 1  # July 1 is past the 90-day cutoff of July 3
    assert result["job_runs_deleted"] == 1
    assert query(database, "SELECT at FROM test_events") == [(recent,)]


async def test_reaper_gives_uploads_longer(database, db):
    prepare(database, ("DE",), DE=time(12))
    a = issue_token(database, provider="alpha", product="res", region="fra1")
    await batches.census_plan(db, NOW)
    batch_id = query(database, "SELECT batch_id FROM test_batches")[0][0]
    node = {"node_id": a["node_id"]}
    start = NOW + timedelta(seconds=30)
    await nodework.assignments(db, node, start)
    assert (await nodework.begin_upload(db, node, batch_id, start))["status"] == "running"
    assert (await nodework.reap(db, start + timedelta(minutes=20)))["failed"] == {}
    assert (await nodework.reap(db, start + timedelta(minutes=46)))["failed"] == {"stale": 1}


def test_cli_node_token_and_dev_bind(database, capsys):
    from proxy_index.cli import main

    prepare(database)
    assert (
        main(
            [
                "node-token",
                "--database-file",
                str(database),
                "--provider",
                "alpha",
                "--product",
                "res",
                "--region",
                "prod-fra1-a",
            ]
        )
        == 0
    )
    issued = json.loads(capsys.readouterr().out)
    assert issued["region"] == "fra1" and len(issued["token"]) > 30 and not issued["rotated"]
    assert query(database, "SELECT token_hash<>%s FROM nodes", (issued["token"],)) == [(True,)]
    assert (
        main(["orchestrator", "--dev", "--host", "0.0.0.0", "--database-file", str(database)]) == 2
    )
    assert (
        main(
            [
                "node-token",
                "--database-file",
                str(database),
                "--provider",
                "alpha",
                "--product",
                "res",
                "--region",
                "mars1",
            ]
        )
        == 2
    )
