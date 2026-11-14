"""The orchestrator's scheduled jobs.

census_plan / pulse_plan  every minute, on the minute: open due batches (batches.py)
reap                      every minute: fail runs that never started or went silent (nodework.py)
offline                   every 15 s: nodes without a heartbeat go offline
calculate                 every minute, due by calculate.trigger: "hourly" (default) once per
                          UTC hour, calculate.hour_offset_seconds past it (at least
                          MIN_HOUR_OFFSET_S, so the hour just released is in), or "batch"
                          debounced to calculate.min_interval_seconds and only after a batch
                          finished; runs `atlas calculate --publish` as a subprocess,
                          killed after calculate.timeout_seconds, under
                          `systemd-run --scope --unit=atlas-calculate -p MemoryMax=…`
                          when $ATLAS_CALCULATE_MEMORY_MAX is set (the manager's unit; auto:
                          sizing.scope_memory); its timing lines are logged
estimates_release         every minute: estimates.release.release_due(db) releases the estimated
                          hours that ended, with their estimate_hour_released events
detect                    every minute: atlas.detect.detect(conn, now) writes the
                          incident events from finished pulse batches (optional module)
retention                 daily: test_events older than retention.events_days (whole monthly
                          partitions are dropped), job_runs older than retention.job_runs_days,
                          estimated_hours older than retention.estimates_days (not while
                          archive.enabled: the archive deletes them with their run)
archive                   daily at 00:40 UTC, when `archive.enabled`: atlas.archive.job
                          archives the due days to Spaces and deletes their raw rows
raw_upload                every 5 minutes, when `archive.enabled` and `raw.spool_dir`: each
                          ended hour's raw files to Spaces, one zstd tar per network
                          (atlas.archive.rawhours), hours older than 30 days removed
reports                   every 30 s, when the `reports.dir` setting is set: index.html and a
                          live page for every running batch (report_html.write(live=True))
dbip_update               daily at 05:23 UTC, when the `dbip.dir` setting is set: dbip.update
                          renames the newest dated DB-IP MMDB from `dbip.base_url` over
                          `dbip.dir`/dbip-full.mmdb and records it; unchanged is 'skipped'
browser_plan              every minute, on the minute, while `browser.enabled` and the settings
                          file at `browser.settings_file` say so: open each browser target's
                          slot in the browser region (browser.py)

job_table.register_jobs adds them to the scheduler. Besides the job, when a batch closes and
`reports.dir` is set, the HTML report of that batch and the index are written there
(report_html.write, in a thread; install_reports).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import sys
import weakref
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from psycopg import sql

from .. import sizing, storage, timing
from ..regions import CONFIGS
from .db import Database, setting
from .job_table import register_jobs  # noqa: F401 (callers keep importing it from here)

log = logging.getLogger(__name__)
# One report writer per pool: the close hook and the reports job share it.
_REPORT_LOCKS: weakref.WeakKeyDictionary[Database, asyncio.Lock] = weakref.WeakKeyDictionary()
METHODOLOGY = CONFIGS / "methodology.toml"
PARTITION = re.compile(r"test_events_(\d{4})_(\d{2})")
# The estimates_release job runs this often, so hour H is released by H+1:00 + RELEASE_EVERY_S.
# The calculator floors as_of to 300 s and includes only hours released by as_of: an hourly
# calculation sees hour H only at H+1:00 + 300 s + RELEASE_EVERY_S or later.
RELEASE_EVERY_S = 60
MIN_HOUR_OFFSET_S = 300 + RELEASE_EVERY_S
# The calculation's memory cap (a systemd size such as 8G, or auto), set by the manager's unit.
MEMORY_ENV = "ATLAS_CALCULATE_MEMORY_MAX"
MEMORY_RE = re.compile(r"[1-9][0-9]*[KMGT]?")
# The capped calculation's scope. One fixed name: while a calculation left by a crashed or killed
# orchestrator still runs (its scope is outside the service), systemd refuses a second one, and
# the job fails with that exit instead of running two at once. CollectMode unloads the scope as
# soon as it ends, also after an out-of-memory kill (recorded as exit 137), so the name is free
# for the next hour: a failed scope that stayed loaded would refuse every later calculation.
SCOPE_UNIT = "atlas-calculate"
# MANAGER-HOST-1: on an existing server the orchestrator is a user unit that names its
# environment's scope here (atlas-calculate, atlas-staging-calculate), a scope of the user's.
SCOPE_ENV = "ATLAS_CALCULATE_SCOPE"
SCOPE_RE = re.compile(r"atlas(-staging)?-calculate")


class CalculationFailed(RuntimeError):
    """The calculation subprocess failed: its exit code, or the timeout, and stderr's last line."""


def _batch_skip(state: dict, now: datetime, interval: timedelta) -> str | None:
    """The old schedule: after a batch finished, at most once per interval."""
    last, finished = state["last"], state["finished"]
    if finished is None or (last is not None and finished <= last):
        return "no batch finished since the last calculation"
    if last is not None and now - last < interval:
        return "debounced"
    return None


def _hourly_skip(state: dict, now: datetime, offset: timedelta) -> str | None:
    """Once per UTC hour, `offset` past the hour (when that hour's estimate is released)."""
    slot = (now - offset).replace(minute=0, second=0, microsecond=0) + offset
    if state["last"] is not None and state["last"] >= slot:
        return "already calculated this hour"
    return None


async def _calculate_skip(conn, now: datetime) -> str | None:
    trigger = await setting(conn, "calculate.trigger")
    state = await (
        await conn.execute(
            "SELECT (SELECT max(started_at) FROM job_runs WHERE job='calculate' "
            "AND status='ok') AS last,(SELECT max(finished_at) FROM test_batches "
            "WHERE status='done') AS finished"
        )
    ).fetchone()
    if trigger == "hourly":
        offset = float(await setting(conn, "calculate.hour_offset_seconds"))
        if offset < MIN_HOUR_OFFSET_S:
            log.warning(
                "calculate.hour_offset_seconds %s would leave out the hour just released; using %s",
                offset,
                MIN_HOUR_OFFSET_S,
            )
            offset = MIN_HOUR_OFFSET_S
        return _hourly_skip(state, now, timedelta(seconds=offset))
    if trigger == "batch":
        interval = float(await setting(conn, "calculate.min_interval_seconds"))
        return _batch_skip(state, now, timedelta(seconds=interval))
    raise ValueError(f"calculate.trigger must be 'hourly' or 'batch', not {trigger!r}")


async def calculate(db: Database, credentials_file: str, methodology: Path) -> dict[str, Any]:
    now = datetime.now(UTC)
    async with db.connection() as conn:
        skipped = await _calculate_skip(conn, now)
        timeout = float(await setting(conn, "calculate.timeout_seconds"))
    if skipped:
        return {"skipped": skipped}
    memory = sizing.scope_memory(os.environ.get(MEMORY_ENV))
    argv = calculation_argv(credentials_file, methodology, memory, os.environ.get(SCOPE_ENV))
    return await run_calculation(argv, timeout)


def calculation_argv(
    credentials_file: str,
    methodology: Path | str,
    memory_max: str | None = None,
    user_scope: str | None = None,
) -> list[str]:
    """`atlas calculate --publish --incremental` of this installation (explicit, so the hourly
    job never depends on the CLI's default), under the systemd scope SCOPE_UNIT capped at
    `memory_max` when given (the database file is passed by path, never read here). With
    `user_scope` the scope is that user scope instead (`systemd-run --user`)."""
    script = Path(sys.executable).with_name("atlas")
    command = (
        [str(script)]
        if script.exists()
        else [
            sys.executable,
            "-c",
            "import sys; from atlas.cli import main; sys.exit(main())",
        ]
    )
    command += ["calculate", "--database-file", str(credentials_file)]
    command += ["--methodology", str(methodology), "--publish", "--incremental"]
    if not memory_max:
        return command
    if not MEMORY_RE.fullmatch(memory_max):
        raise ValueError(f"{MEMORY_ENV} must be a size such as 8G, or auto")
    if user_scope and not SCOPE_RE.fullmatch(user_scope):
        raise ValueError(f"{SCOPE_ENV} must be atlas-calculate or atlas-staging-calculate")
    user = ["--user"] if user_scope else []
    scope = ["systemd-run", *user, "--scope", "--quiet", f"--unit={user_scope or SCOPE_UNIT}"]
    limits = ["-p", f"MemoryMax={memory_max}", "-p", "CollectMode=inactive-or-failed"]
    return [*scope, *limits, "--", *command]


async def run_calculation(argv: list[str], limit_s: float) -> dict[str, Any]:
    """Run the calculation; its stdout JSON gives the snapshot. A non-zero exit or the timeout
    (the whole process group is killed) raises CalculationFailed, which the job records."""
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), limit_s)
    except BaseException as exc:  # boundary: a timeout or a stop kills the group; re-raised
        _kill_group(process.pid)
        await process.wait()
        if isinstance(exc, TimeoutError):
            raise CalculationFailed(f"timed out after {limit_s:g} s (killed)") from None
        raise
    text = err.decode(errors="replace")
    for line in timing.lines(text):  # the calculation's stages, into this service's journal
        log.info("%s", line)
    lines = text.strip().splitlines()
    tail = lines[-1][:200] if lines else ""
    if process.returncode != 0:
        raise CalculationFailed(f"exit {process.returncode}: {tail}".rstrip(": "))
    try:
        payload = json.loads(out)
        snapshot, as_of = payload["snapshot_id"], payload["as_of"]
    except (ValueError, TypeError, KeyError):
        raise CalculationFailed("exit 0 without the JSON result on stdout") from None
    return {"snapshot_id": snapshot, "as_of": str(as_of), "exit_code": 0}


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


async def detect(credentials_file: str) -> dict[str, Any]:
    try:
        from ..detect import detect as run_detect
    except ModuleNotFoundError:
        return {"skipped": "atlas.detect is not installed"}

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
        events = (await conn.execute("DELETE FROM test_events WHERE at<%s", (cutoff,))).rowcount
        jobs = (
            await conn.execute(
                "DELETE FROM job_runs WHERE started_at<%s AND status<>'running'",
                (now - timedelta(days=job_days),),
            )
        ).rowcount
        estimates_days = int(await setting(conn, "retention.estimates_days"))
        estimates = 0
        # With the raw archive on, estimated hours go only with their run, after its day file
        # is verified in the bucket (ARCHIVE-1); this fallback would delete them unarchived.
        if not await setting(conn, "archive.enabled"):
            estimates = (
                await conn.execute(
                    "DELETE FROM estimated_hours WHERE day<%s",
                    ((now - timedelta(days=estimates_days)).date(),),
                )
            ).rowcount
    return {
        "dropped_partitions": dropped,
        "events_deleted": events,
        "job_runs_deleted": jobs,
        "estimated_hours_deleted": estimates,
    }


async def archive(db: Database, credentials_file: str) -> dict[str, Any]:
    from ..archive.job import run_due
    from ..archive.spaces import Spaces

    async with db.connection() as conn:
        if not await setting(conn, "archive.enabled"):
            return {"skipped": "archive.enabled is off"}
        spaces_file = await setting(conn, "archive.spaces_file")
        work_dir = Path(await setting(conn, "archive.work_dir"))
    spaces = await asyncio.to_thread(Spaces.from_file, spaces_file)
    return await asyncio.to_thread(run_due, credentials_file, spaces, work_dir, datetime.now(UTC))


async def raw_upload(db: Database) -> dict[str, Any]:
    from ..archive.rawhours import upload_hours
    from ..archive.spaces import Spaces

    async with db.connection() as conn:
        root = await setting(conn, "raw.spool_dir")
        if not (await setting(conn, "archive.enabled") and root):
            return {"skipped": "archive.enabled or raw.spool_dir is off"}
        spaces_file = await setting(conn, "archive.spaces_file")
    spaces = await asyncio.to_thread(Spaces.from_file, spaces_file)
    return await asyncio.to_thread(upload_hours, root, spaces, datetime.now(UTC))


async def write_reports(db: Database, batch_ids: list[int]) -> dict[str, Any] | None:
    """The HTML reports for batches that just closed, and the index; None when reports.dir is
    unset. Errors are logged by type, never raised: a report must not fail the barrier."""
    try:
        async with db.connection() as conn:
            out = await setting(conn, "reports.dir")
        if not out:
            return None
        from .. import report_html

        async with _report_lock(db):
            return await asyncio.to_thread(report_html.write, db.credentials_file, out, batch_ids)
    except Exception as exc:  # boundary: a report must not fail the barrier; type only (DSN)
        error = type(exc).__name__
        log.warning("could not write the HTML reports for batches %s (%s)", batch_ids, error)
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


async def dbip_update(db: Database, credentials_file: str) -> dict[str, Any]:
    """The daily DB-IP update; a failure fails the job (and alerts)."""
    async with db.connection() as conn:
        dest = await setting(conn, "dbip.dir")
        configured = await setting(conn, "dbip.base_url")
    if not dest:
        return {"skipped": "dbip.dir is not set"}
    from .. import dbip

    def work() -> dict[str, Any]:
        base = dbip.base_url(configured)
        return dbip.update(
            Path(dest), base, database_file=credentials_file, checks=dbip.DEFAULT_CHECKS
        )

    try:
        result = await asyncio.to_thread(work)
    except dbip.Busy:
        return {"skipped": "another update is running"}
    if result.get("error"):
        raise dbip.UpdateError(result["error"])
    if result["dbip"] == "unchanged":
        return {"skipped": "release unchanged", "release": result["source"]["name"]}
    return {"dbip": result["dbip"], "release": result["source"]["name"], "sha256": result["sha256"]}


def install_reports(db: Database) -> None:
    """Write reports whenever a committed transaction closes batches (Database.on_close)."""

    async def hook(batch_ids: list[int]) -> None:
        await write_reports(db, batch_ids)

    db.on_close["reports"] = hook
