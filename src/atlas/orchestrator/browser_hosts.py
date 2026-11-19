"""Browser hosts' run limit and stuck runs (BROWSER-HOST-LIMIT-1, docs/browser.html, Scheduling).

A host runs at most `browsers_per_host` browser runs at once. Its open runs are those of batches
starting within OPEN_AHEAD (or earlier): 'running' ones (handed to its node, started or not), and
'queued' ones that began at most batch.start_grace_seconds ago (later the reaper fails them as
not started); a run handed out ahead for a later start counts only from then. The
queue planner opens an entry, or decides its catch-up, only while its host has fewer than that;
otherwise the entry waits for a later tick, and one missed by more than GRACE goes the usual
catch-up-or-skip way (browser.plan_queue). A catch-up decided earlier for a later start is
counted only from OPEN_AHEAD before it, so it may still put its host one over: the alert
browser_host_over_limit (major, one per host, its occurrences count the ticks) says so while a
host has more open runs than its limit, and is acknowledged once it has no more.

A browser run's last_progress_at moves only when its counters do (nodework.progress), so a run
whose node still posts the same counters is stuck. The reaper fails a browser run with no
progress for browser.stall_minutes (error 'stale') or, in queue mode, still running
run_deadline_minutes after its start (error 'deadline'), which frees its place on the host;
each raises the alert browser_run_abandoned (major, one per host and UTC day, its occurrences
count the runs). The node is told to stop at its next progress post, unless the run was failed
only for its silence (stale, not_started) and its deadline has not passed: then it may finish and
upload late (nodework._finishing, BROWSER-COVERAGE-1).
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

import psycopg

from ..browser.settings import BrowserSettings
from .db import setting
from .events import raise_alert
from .sqlrows import _rows

OPEN_AHEAD = timedelta(minutes=2)  # a queue entry's batch opens this long before its start
NEVER = datetime.min.replace(tzinfo=UTC)  # a cutoff no run starts before
OVER_ALERT = "browser_host_over_limit"
ABANDONED_ALERT = "browser_run_abandoned"
REASONS = {"stale": "made no progress", "deadline": "passed its deadline"}


async def open_runs(
    conn: psycopg.AsyncConnection, now: datetime
) -> tuple[Counter[str], set[tuple[str, str]]]:
    """Each browser host's open runs (see the module doc), and the (host, network) of each."""
    grace = timedelta(seconds=float(await setting(conn, "batch.start_grace_seconds")))
    rows = await _rows(
        conn,
        "SELECT n.host,n.provider||'/'||n.product AS network,count(*) AS runs "
        "FROM batch_runs br JOIN test_batches b USING(batch_id) "
        "JOIN nodes n USING(node_id) WHERE b.kind='browser' AND n.host IS NOT NULL "
        "AND b.status IN ('scheduled','running') AND b.start_at<=%s AND (br.status='running' "
        "OR (br.status='queued' AND b.start_at>=%s)) GROUP BY 1,2",
        (now + OPEN_AHEAD, now - grace),
    )
    runs: Counter[str] = Counter()
    for row in rows:
        runs[row["host"]] += row["runs"]
    return runs, {(row["host"], row["network"]) for row in rows}


async def check_limit(conn: psycopg.AsyncConnection, runs: Counter[str], limit: int) -> list[str]:
    """The hosts over `limit`, alerted; the alert of every other host is acknowledged."""
    over = sorted(host for host, count in runs.items() if count > limit)
    keys = [f"{OVER_ALERT}:{host}" for host in over]
    await conn.execute(
        "UPDATE alerts SET acknowledged_at=now() WHERE kind=%s AND acknowledged_at IS NULL "
        "AND NOT dedupe_key=ANY(%s)",
        (OVER_ALERT, keys),
    )
    for host, key in zip(over, keys, strict=True):
        await raise_alert(
            conn,
            OVER_ALERT,
            f"{host} has {runs[host]} browser runs open, limit {limit}",
            severity="major",
            dedupe_key=key,
            data={"host": host, "runs": runs[host], "limit": limit},
        )
    return over


async def reap_cutoffs(conn: psycopg.AsyncConnection, now: datetime) -> dict[str, datetime]:
    """The reaper's browser cutoffs: no progress since `browser_stale`, started before
    `deadline`: run_deadline_minutes of the settings file in queue mode. In slots mode a run's
    deadline is its slot's end, and without usable settings it is unknown: no deadline cutoff
    then (NEVER), only the stall rule."""
    from .browser import _settings

    stall = float(await setting(conn, "browser.stall_minutes"))
    path = await setting(conn, "browser.settings_file")
    loaded = _settings(str(path)) if path else None
    deadline = NEVER
    if isinstance(loaded, BrowserSettings) and loaded.schedule.plan == "queue":
        deadline = now - timedelta(minutes=loaded.schedule.run_deadline_minutes)
    return {"browser_stale": now - timedelta(minutes=stall), "deadline": deadline}


async def abandoned(conn: psycopg.AsyncConnection, batch_id: int, run: dict, now: datetime) -> None:
    """The alert of a browser run the reaper failed as stale or past its deadline."""
    host, day = run["host"] or f"node {run['node_id']}", now.date().isoformat()
    await raise_alert(
        conn,
        ABANDONED_ALERT,
        f"{host}: browser run {batch_id} ({run['target_id']}) {REASONS[run['error']]}; abandoned",
        severity="major",
        dedupe_key=f"{ABANDONED_ALERT}:{host}:{day}",
        data={"host": host, "batch_id": batch_id, "error": run["error"]},
    )
