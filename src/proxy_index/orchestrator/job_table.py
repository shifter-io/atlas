"""The job table: every scheduled job of the orchestrator, with its schedule (jobs.py lists them).

The job functions live in jobs.py, which re-exports register_jobs; it is imported when the jobs
are registered, so either module can be imported first.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import batches, nodework, pulse
from .browser import browser_plan
from .db import Database
from .scheduler import Scheduler


def register_jobs(
    scheduler: Scheduler, db: Database, credentials_file: str, methodology: Path | None = None
) -> None:
    from . import jobs

    methodology = methodology or jobs.METHODOLOGY
    jobs.install_reports(db)
    _register_planners(scheduler, db)
    _register_data_jobs(scheduler, db, credentials_file, methodology)

    async def browser_plan_job() -> dict[str, Any]:
        return await browser_plan(db)

    scheduler.add("browser_plan", browser_plan_job, every_s=60, aligned=True)


def _register_planners(scheduler: Scheduler, db: Database) -> None:
    async def census_plan() -> dict[str, Any]:
        return await batches.census_plan(db)

    async def pulse_plan() -> dict[str, Any]:
        return await batches.pulse_plan(db)

    async def pulse_evaluate() -> dict[str, Any]:
        return await pulse.evaluate(db)

    async def reap() -> dict[str, Any]:
        return await nodework.reap(db)

    async def offline() -> dict[str, Any]:
        return await nodework.mark_offline(db)

    scheduler.add("census_plan", census_plan, every_s=60, aligned=True)
    scheduler.add("pulse_plan", pulse_plan, every_s=60, aligned=True)
    scheduler.add("pulse_evaluate", pulse_evaluate, every_s=60)
    scheduler.add("reap", reap, every_s=60)
    scheduler.add("offline", offline, every_s=15)


def _register_data_jobs(
    scheduler: Scheduler, db: Database, credentials_file: str, methodology: Path
) -> None:
    from . import jobs

    async def calculate_job() -> dict[str, Any]:
        return await jobs.calculate(db, credentials_file, methodology)

    async def estimates_release_job() -> dict[str, Any]:
        from ..estimates.release import release_due

        return {"released": len(await release_due(db))}

    async def detect_job() -> dict[str, Any]:
        return await jobs.detect(credentials_file)

    async def retention_job() -> dict[str, Any]:
        return await jobs.retention(db)

    async def archive_job() -> dict[str, Any]:
        return await jobs.archive(db, credentials_file)

    async def raw_upload_job() -> dict[str, Any]:
        return await jobs.raw_upload(db)

    async def reports_job() -> dict[str, Any]:
        return await jobs.live_reports(db)

    async def dbip_job() -> dict[str, Any]:
        return await jobs.dbip_update(db, credentials_file)

    scheduler.add("calculate", calculate_job, every_s=60)
    scheduler.add("estimates_release", estimates_release_job, every_s=jobs.RELEASE_EVERY_S)
    scheduler.add("detect", detect_job, every_s=60)
    scheduler.add("retention", retention_job, at_utc_minute=3 * 60 + 17)
    scheduler.add("archive", archive_job, at_utc_minute=40)
    scheduler.add("raw_upload", raw_upload_job, every_s=300)
    scheduler.add("reports", reports_job, every_s=30)
    scheduler.add("dbip_update", dbip_job, at_utc_minute=5 * 60 + 23)
