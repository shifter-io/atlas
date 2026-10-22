"""The orchestrator's scheduled jobs.

census_plan / pulse_plan  every minute, on the minute: open due batches (batches.py)
reap                      every minute: fail runs that never started or went silent
offline                   every 15 s: nodes without a heartbeat go offline
calculate                 every minute, debounced to calculate.min_interval_seconds and only
                          after a batch finished: calculated.calculator.calculate(publish=True)
detect                    every minute: proxy_index.detect.detect(conn, now) writes the
                          incident events from finished pulse batches (optional module)
retention                 daily: test_events older than retention.events_days (whole monthly
                          partitions are dropped), job_runs older than retention.job_runs_days
reports                   every 30 s, when the `reports.dir` setting is set: index.html and a
                          live page for every running batch (report_html.write(live=True))

Besides the job, when a batch closes and `reports.dir` is set, the HTML report of that batch
and the index are written there (report_html.write, in a thread; install_reports).
"""

from __future__ import annotations

import asyncio
import logging
import re
import weakref
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from psycopg import sql

from .. import storage
from ..regions import CONFIGS
from . import batches
from .db import Database, setting
from .scheduler import Scheduler

log = logging.getLogger(__name__)
# One report writer per pool: the close hook and the reports job share it.
_REPORT_LOCKS: weakref.WeakKeyDictionary[Database, asyncio.Lock] = weakref.WeakKeyDictionary()
METHODOLOGY = CONFIGS / "methodology.toml"
PARTITION = re.compile(r"test_events_(\d{4})_(\d{2})")


async def calculate(db: Database, credentials_file: str, methodology: Path) -> dict[str, Any]:
    async with db.connection() as conn:
        interval = timedelta(seconds=float(await setting(conn, "calculate.min_interval_seconds")))
        state = await (
            await conn.execute(
                "SELECT (SELECT max(started_at) FROM job_runs WHERE job='calculate' "
                "AND status='ok') AS last,(SELECT max(finished_at) FROM test_batches "
                "WHERE status='done') AS finished"
            )
        ).fetchone()
    now = datetime.now(UTC)
    last, finished = state["last"], state["finished"]
    if finished is None or (last is not None and finished <= last):
        return {"skipped": "no batch finished since the last calculation"}
    if last is not None and now - last < interval:
        return {"skipped": "debounced"}
    from ..calculated.calculator import calculate as run_calculation

    payload = await asyncio.to_thread(
        run_calculation, credentials_file, str(methodology), publish=True
    )
    return {"snapshot_id": payload.get("snapshot_id"), "as_of": str(payload.get("as_of"))}


async def detect(credentials_file: str) -> dict[str, Any]:
    try:
        from ..detect import detect as run_detect
    except ModuleNotFoundError:
        return {"skipped": "proxy_index.detect is not installed"}

    def work() -> int:
        with storage.connect(credentials_file) as conn:
            return run_detect(conn, datetime.now(UTC))

    return {"events": await asyncio.to_thread(work)}


async def retention(db: Database, now: datetime | None = None) -> dict[str, Any]:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    async with db.connection() as conn:
        days = int(await setting(conn, "retention.events_days"))
        job_days = int(await setting(conn, "retention.job_runs_days"))
        cutoff = now - timedelta(days=days)
        partitions = await (
            await conn.execute(
                "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid=i.inhrelid "
                "WHERE i.inhparent='test_events'::regclass ORDER BY c.relname"
            )
        ).fetchall()
        dropped = []
        for row in partitions:
            match = PARTITION.fullmatch(row["relname"])
            if not match:
                continue
            start = datetime(int(match[1]), int(match[2]), 1, tzinfo=UTC)
            end = (start + timedelta(days=32)).replace(day=1)
            if end <= cutoff:
                await conn.execute(sql.SQL("DROP TABLE {}").format(sql.Identifier(row["relname"])))
                dropped.append(row["relname"])
        events = (
            await conn.execute("DELETE FROM test_events WHERE at<%s", (cutoff,))
        ).rowcount
        jobs = (
            await conn.execute(
                "DELETE FROM job_runs WHERE started_at<%s AND status<>'running'",
                (now - timedelta(days=job_days),),
            )
        ).rowcount
    return {"dropped_partitions": dropped, "events_deleted": events, "job_runs_deleted": jobs}


async def write_reports(db: Database, batch_ids: list[int]) -> dict[str, Any] | None:
    """The HTML reports for batches that just closed, and the index; None when reports.dir is
    unset. Errors are logged, never raised: a report must not fail the barrier."""
    try:
        async with db.connection() as conn:
            out = await setting(conn, "reports.dir")
        if not out:
            return None
        from .. import report_html

        async with _report_lock(db):
            return await asyncio.to_thread(
                report_html.write, db.credentials_file, out, batch_ids
            )
    except Exception:  # noqa: BLE001
        log.exception("could not write the HTML reports for batches %s", batch_ids)
        return None


def _report_lock(db: Database) -> asyncio.Lock:
    lock = _REPORT_LOCKS.get(db)
    if lock is None:
        lock = _REPORT_LOCKS[db] = asyncio.Lock()
    return lock


async def live_reports(db: Database) -> dict[str, Any]:
    """The `reports` job: index.html and the live page of every running batch."""
    async with db.connection() as conn:
        out = await setting(conn, "reports.dir")
    if not out:
        return {"skipped": "reports.dir is not set"}
    from .. import report_html

    async with _report_lock(db):
        result = await asyncio.to_thread(
            report_html.write, db.credentials_file, out, None, live=True
        )
    return {"dir": result["dir"], "running": len(result["batches"])}


def install_reports(db: Database) -> None:
    """Write reports whenever a committed transaction closes batches (Database.on_close)."""

    async def hook(batch_ids: list[int]) -> None:
        await write_reports(db, batch_ids)

    db.on_close["reports"] = hook


def register_jobs(
    scheduler: Scheduler, db: Database, credentials_file: str, methodology: Path | None = None
) -> None:
    methodology = methodology or METHODOLOGY
    install_reports(db)

    async def census_plan() -> dict[str, Any]:
        return await batches.census_plan(db)

    async def pulse_plan() -> dict[str, Any]:
        return await batches.pulse_plan(db)

    async def reap() -> dict[str, Any]:
        return await batches.reap(db)

    async def offline() -> dict[str, Any]:
        return await batches.mark_offline(db)

    async def calculate_job() -> dict[str, Any]:
        return await calculate(db, credentials_file, methodology)

    async def detect_job() -> dict[str, Any]:
        return await detect(credentials_file)

    async def retention_job() -> dict[str, Any]:
        return await retention(db)

    async def reports_job() -> dict[str, Any]:
        return await live_reports(db)

    scheduler.add("census_plan", census_plan, every_s=60, aligned=True)
    scheduler.add("pulse_plan", pulse_plan, every_s=60, aligned=True)
    scheduler.add("reap", reap, every_s=60)
    scheduler.add("offline", offline, every_s=15)
    scheduler.add("calculate", calculate_job, every_s=60)
    scheduler.add("detect", detect_job, every_s=60)
    scheduler.add("retention", retention_job, at_utc_minute=3 * 60 + 17)
    scheduler.add("reports", reports_job, every_s=30)
