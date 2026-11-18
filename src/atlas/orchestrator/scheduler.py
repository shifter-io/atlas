"""The scheduler, ported from the collector.

A small clock rather than a cron dependency: every job is a coroutine with a fire time, each
run is recorded in `job_runs`, and a job that raises is logged and alerted rather than taking
the process down. A job still running when it is due again is skipped (and recorded so); a task
with stuck_after_s raises the alert job_stuck (major, one per run) on such a skip once its run
has gone on that long.

A failed job raises job_failed, one alert per job (dedupe key job_failed:<job>), or per the
exception's own `dedupe_key` when it has one (raw_upload: per failure kind and hour).

A job may return {"skipped": "<reason>"} to record a fire that decided there was nothing to do
(the debounced calculate, a planner switched off) as 'skipped' instead of 'ok'.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .db import Database
from .events import raise_alert

log = logging.getLogger(__name__)

Job = Callable[[], Awaitable[dict[str, Any] | None]]


@dataclass(slots=True)
class Task:
    name: str
    job: Job
    # Seconds between runs. With `aligned`, fire on multiples of every_s since the epoch.
    every_s: float | None = None
    aligned: bool = False
    # Minute of the hour, for jobs that must align with the clock.
    at_minute: int | None = None
    # Minute of the UTC day, for once-a-day jobs.
    at_utc_minute: int | None = None
    # A run still going this long is reported stuck (job_stuck) at the next skipped fire.
    stuck_after_s: float | None = None
    next_at: datetime | None = None
    running: bool = False
    began: float = 0.0  # perf_counter of the running run's start
    run_id: Any = None  # job_runs id of the running run
    last: dict[str, Any] = field(default_factory=dict)

    def schedule(self, now: datetime) -> datetime:
        if self.at_minute is not None:
            candidate = now.replace(minute=self.at_minute, second=0, microsecond=0)
            if candidate <= now:
                candidate += timedelta(hours=1)
            return candidate
        if self.at_utc_minute is not None:
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            candidate = midnight + timedelta(minutes=self.at_utc_minute)
            if candidate <= now:
                candidate += timedelta(days=1)
            return candidate
        every = self.every_s or 60
        if self.aligned:
            stamp = (now.timestamp() // every + 1) * every
            return datetime.fromtimestamp(stamp, UTC)
        return now + timedelta(seconds=every)


@dataclass(slots=True)
class Scheduler:
    """Owns the clock. One instance per orchestrator process."""

    db: Database
    tasks: dict[str, Task] = field(default_factory=dict)
    _runner: asyncio.Task[None] | None = None
    _fired: set[asyncio.Task[Any]] = field(default_factory=set)

    def add(
        self,
        name: str,
        job: Job,
        *,
        every_s: float | None = None,
        aligned: bool = False,
        at_minute: int | None = None,
        at_utc_minute: int | None = None,
    ) -> Task:
        task = self.tasks[name] = Task(
            name=name,
            job=job,
            every_s=every_s,
            aligned=aligned,
            at_minute=at_minute,
            at_utc_minute=at_utc_minute,
        )
        return task

    async def run_now(self, name: str) -> dict[str, Any] | None:
        """Fire a job by hand (the admin "run now" button)."""
        task = self.tasks.get(name)
        if task is None:
            raise KeyError(f"no job named {name!r}")
        return await self._execute(task)

    async def _record(self, sql: str, params: tuple[Any, ...]) -> Any:
        async with self.db.connection() as conn:
            row = await (await conn.execute(sql, params)).fetchone()
        return row

    async def _execute(self, task: Task) -> dict[str, Any] | None:
        if task.running:
            # A slow job must not overlap itself: two calculations would fight over rows.
            log.warning("job %s is still running, skipping this fire", task.name)
            await self._record(
                "INSERT INTO job_runs(job,finished_at,status,error,duration_ms) "
                "VALUES (%s,now(),'skipped','still running',0) RETURNING id",
                (task.name,),
            )
            await self._alert_stuck(task)
            return None
        task.running = True
        task.began = began = time.perf_counter()
        task.run_id = None
        try:
            row = await self._record(
                "INSERT INTO job_runs(job,status) VALUES (%s,'running') RETURNING id",
                (task.name,),
            )
            task.run_id = job_run_id = row["id"]
            try:
                stats = await task.job()
            except Exception as exc:  # boundary: one job must not stop the clock
                await self._record_failure(task, exc, began, job_run_id)
                return None
            await self._record_finish(task, stats, began, job_run_id)
            return stats
        finally:
            task.running = False

    async def _alert_stuck(self, task: Task) -> None:
        running_s = time.perf_counter() - task.began
        if task.stuck_after_s is None or running_s < task.stuck_after_s:
            return
        async with self.db.transaction() as conn:
            await raise_alert(
                conn,
                "job_stuck",
                f"{task.name} still running after {int(running_s // 60)} min",
                severity="major",
                dedupe_key=f"job_stuck:{task.name}:{task.run_id}",
                data={"job": task.name, "job_run_id": task.run_id, "running_s": int(running_s)},
            )

    async def _record_failure(
        self, task: Task, exc: Exception, began: float, job_run_id: Any
    ) -> None:
        log.exception("job %s failed", task.name)
        error = f"{type(exc).__name__}: {exc}"[:500]
        await self._record(
            "UPDATE job_runs SET finished_at=now(),status='failed',error=%s,"
            "duration_ms=%s WHERE id=%s RETURNING id",
            (error, _ms(began), job_run_id),
        )
        kind, key = (getattr(exc, name, None) for name in ("kind", "dedupe_key"))
        kind = kind if isinstance(kind, str) else None
        async with self.db.transaction() as conn:
            await raise_alert(
                conn,
                "job_failed",
                f"{task.name} failed: {type(exc).__name__}" + (f" ({kind})" if kind else ""),
                severity="major",
                dedupe_key=key if isinstance(key, str) else f"job_failed:{task.name}",
                data={"job": task.name, "error": error[:300]},
            )
        task.last = {"status": "failed", "error": error}

    async def _record_finish(
        self, task: Task, stats: dict[str, Any] | None, began: float, job_run_id: Any
    ) -> None:
        # Only a reason string marks a skip: census_plan also reports a count of skipped
        # batches under the same key, and a tick that skipped one overlap still ran.
        reason = stats.get("skipped") if isinstance(stats, dict) else None
        skipped = reason if isinstance(reason, str) and reason else None
        await self._record(
            "UPDATE job_runs SET finished_at=now(),status=%s,error=%s,duration_ms=%s "
            "WHERE id=%s RETURNING id",
            (
                "skipped" if skipped else "ok",
                str(skipped)[:500] if skipped else None,
                _ms(began),
                job_run_id,
            ),
        )
        task.last = {"status": "skipped" if skipped else "ok", **(stats or {})}

    async def _fire(self, task: Task) -> None:
        try:
            await self._execute(task)
        except Exception:  # boundary: the database may be down; the clock keeps going
            log.exception("could not record job %s", task.name)

    async def _loop(self) -> None:
        now = datetime.now(UTC)
        for task in self.tasks.values():
            task.next_at = task.schedule(now)
        while True:
            now = datetime.now(UTC)
            for task in [t for t in self.tasks.values() if t.next_at and t.next_at <= now]:
                task.next_at = task.schedule(now)
                # Fired without awaiting: a long calculation must not delay the planners.
                fired = asyncio.create_task(self._fire(task))
                self._fired.add(fired)
                fired.add_done_callback(self._fired.discard)
            upcoming = [t.next_at for t in self.tasks.values() if t.next_at]
            sleep_for = 1.0
            if upcoming:
                sleep_for = max((min(upcoming) - now).total_seconds(), 0.05)
            await asyncio.sleep(min(sleep_for, 30.0))

    def start(self) -> None:
        if self._runner is None:
            self._runner = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._runner is not None:
            self._runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._runner
            self._runner = None
        fired = list(self._fired)
        for task in fired:
            task.cancel()
        await asyncio.gather(*fired, return_exceptions=True)

    def upcoming(self) -> list[dict[str, Any]]:
        """Next fire time per job, for the admin UI."""
        return [
            {"job": t.name, "next_at": t.next_at, "running": t.running, "last": t.last}
            for t in sorted(self.tasks.values(), key=lambda t: t.name)
        ]


def _ms(began: float) -> int:
    return int((time.perf_counter() - began) * 1000)
