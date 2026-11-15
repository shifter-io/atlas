"""Node-facing batch transitions: what a node's calls (api.py, browser_ingest.py) and the reaper
jobs (jobs.py) drive. Planning and the barrier are in batches.py; the lock order is the same.
How a run ends (finish_run, fail_run) is in nodework_finish.py.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from ..browser.contracts import BROWSER_KIND
from .batches import OPEN_BATCHES, _start, close_batch, run_finished
from .browser_hosts import REASONS, abandoned, reap_cutoffs
from .db import Database, setting
from .events import raise_alert
from .node_offline import mark_offline as mark_offline
from .nodework_finish import COUNTERS, _run
from .nodework_finish import fail_run as fail_run
from .nodework_finish import finish_run as finish_run
from .sqlrows import _now, _row, _rows

# Nodes are handed runs this far ahead of start_at so they can wait on the wall clock.
ASSIGN_AHEAD = timedelta(minutes=5)
# An upload is being received or ingested (a large census takes minutes): allow it longer.
UPLOAD_STALE = 3
# A run's start: its own (a moved pulse run, PULSE-STATE-2) or its batch's.
RUN_START = "coalesce(br.start_at,b.start_at)"
# The runs the reaper fails: never started; silent (no counter moved) for batch.stale_seconds,
# a browser run for browser.stall_minutes; a browser run past its deadline; an upload stuck.
REAPABLE = (
    f"((br.started_at IS NULL AND br.status IN ('queued','running') AND {RUN_START}<%(late)s)"
    " OR (br.started_at IS NOT NULL AND br.status='running' AND (coalesce(br.last_progress_at,"
    "br.started_at)<CASE WHEN b.kind='browser' THEN %(browser_stale)s ELSE %(stale)s END"
    " OR (b.kind='browser' AND b.start_at<%(deadline)s)))"
    " OR (br.status='uploading' AND br.last_progress_at<%(ingest)s))"
)


async def assignments(db: Database, node: dict, now: datetime | None = None) -> dict[str, Any]:
    """Hand a node its queued runs due within ASSIGN_AHEAD (marking them 'running'), and the
    batches it must stop. A run handed out but not yet started is handed out again, so a node
    that restarted while waiting for start_at does not lose it."""
    now = _now(now)
    async with db.transaction() as conn:
        limits = await _run_limits(conn)
        rows = await _claim_runs(conn, node, now)
        kill = await _rows(
            conn,
            "SELECT br.batch_id FROM batch_runs br JOIN test_batches b USING(batch_id) "
            "WHERE br.node_id=%s AND (br.status IN ('killed','skipped') "
            " OR b.status IN ('killed','skipped')) "
            "AND coalesce(br.finished_at,b.finished_at,%s)>%s ORDER BY br.batch_id",
            (node["node_id"], now, now - timedelta(minutes=15)),
        )
    work = [_assignment(row, limits) for row in rows]
    return {"assignments": work, "kill": [row["batch_id"] for row in kill]}


async def _run_limits(conn: psycopg.AsyncConnection) -> dict[str, Any]:
    """The settings an assignment carries: census min_sample and stall, the pulse timeout."""
    min_sample = int(await setting(conn, "census.min_sample"))
    stall = float(await setting(conn, "census.stall_seconds"))
    return {
        "min_sample": min_sample,
        "stall_seconds": int(stall) if stall.is_integer() else stall,
        "pulse_timeout": int(await setting(conn, "pulse.timeout_ms")),
    }


async def _claim_runs(conn: psycopg.AsyncConnection, node: dict, now: datetime) -> list[dict]:
    """The node's runs due within ASSIGN_AHEAD, locked; the queued ones become 'running'."""
    rows = await _rows(
        conn,
        f"SELECT b.batch_id,b.kind,b.country_code,b.target_id,{RUN_START} AS start_at,"
        " r.slug AS region,"
        " br.product_id,p.provider,p.name,t.url,t.timeout_ms,t.census_concurrency,"
        " t.pulse_requests,t.pulse_concurrency "
        "FROM batch_runs br JOIN test_batches b USING(batch_id) "
        "JOIN products p ON p.product_id=br.product_id "
        "JOIN targets t ON t.target_id=b.target_id "
        "JOIN runner_regions r ON r.region_id=b.region_id "
        "WHERE br.node_id=%s AND b.status IN ('scheduled','running') "
        " AND (br.status='queued' OR (br.status='running' AND br.started_at IS NULL)) "
        f" AND {RUN_START}<=%s ORDER BY {RUN_START},b.batch_id FOR UPDATE OF br SKIP LOCKED",
        (node["node_id"], now + ASSIGN_AHEAD),
    )
    if rows:
        await conn.execute(
            "UPDATE batch_runs SET status='running' WHERE node_id=%s AND batch_id=ANY(%s) "
            "AND status='queued'",
            (node["node_id"], [row["batch_id"] for row in rows]),
        )
    return rows


def _assignment(row: dict, limits: dict[str, Any]) -> dict[str, Any]:
    """One claimed run as the node receives it."""
    census = row["kind"] == "census"
    # The load wave: census concurrency, one request per connection, counted like a pulse.
    load = row["kind"] == "load"
    return {
        "batch_id": row["batch_id"],
        "kind": row["kind"],
        "product_id": row["product_id"],
        "provider": row["provider"],
        "product": row["name"],
        "country_code": row["country_code"].strip(),
        "region": row["region"],
        "target_id": row["target_id"],
        "target_url": row["url"] or None,
        "timeout_ms": limits["pulse_timeout"] if row["kind"] == "pulse" else row["timeout_ms"],
        "concurrency": row["census_concurrency"] if census or load else row["pulse_concurrency"],
        "stop_rule": "unique_ratio" if census else "count",
        "requests": None
        if census
        else row["census_concurrency"]
        if load
        else row["pulse_requests"],
        "min_sample": limits["min_sample"] if census else None,
        "stall_seconds": limits["stall_seconds"] if census else None,
        "start_at": row["start_at"].astimezone(UTC).isoformat(),
    }


def _counters(values: dict[str, Any]) -> dict[str, int]:
    result = {}
    for key in COUNTERS:
        value = values.get(key, 0)
        if value is None:
            value = 0
        if type(value) is not int or value < 0:
            raise ValueError(f"Counter {key} must be a non-negative integer")
        result[key] = value
    return result


async def progress(
    db: Database, node: dict, batch_id: int, counters: dict[str, Any], now: datetime | None = None
) -> dict[str, Any] | None:
    """Live counters. Returns None for an unknown run, else {"stop": bool}: a run that was
    killed, skipped or failed by the reaper must stop."""
    now = _now(now)
    values = _counters(counters)
    await db.ensure_partition("test_events", now)
    async with db.transaction() as conn:
        run = await _run(conn, node, batch_id)
        if run is None:
            return None
        if run["started_at"] is None and run["status"] == "running":
            await _start(conn, batch_id, run["product_id"], now)
        # last_progress_at moves only with the counters: a hung run posting the same ones is
        # silent (the reaper's stale rule), however often its node posts them.
        given = ",".join(f"%({key})s::bigint" for key in COUNTERS)
        moved = f"({','.join(COUNTERS)}) IS DISTINCT FROM ({given})"
        updated = await _row(
            conn,
            f"UPDATE batch_runs SET last_progress_at=CASE WHEN {moved} THEN %(now)s "
            "ELSE last_progress_at END,"
            + ",".join(f"{key}=%({key})s" for key in COUNTERS)
            + " WHERE batch_id=%(batch)s AND product_id=%(product)s AND status='running' "
            "RETURNING status",
            values | {"now": now, "batch": batch_id, "product": run["product_id"]},
        )
    stop = updated is None or run["batch_status"] in ("killed", "skipped", "failed")
    return {"stop": stop}


async def begin_upload(db: Database, node: dict, batch_id: int, now: datetime | None = None):
    """Mark a run 'uploading'. Returns the run row as it was, or None when unknown."""
    now = _now(now)
    async with db.transaction() as conn:
        run = await _run(conn, node, batch_id)
        if run is not None and run["status"] in ("running", "uploading"):
            await conn.execute(
                "UPDATE batch_runs SET status='uploading',last_progress_at=%s,"
                "started_at=coalesce(started_at,%s) WHERE batch_id=%s AND product_id=%s",
                (now, now, batch_id, run["product_id"]),
            )
        return run


# --- reaper ------------------------------------------------------------------------------


async def reap(db: Database, now: datetime | None = None) -> dict[str, Any]:
    """Fail runs that never started or went silent, then close what that completes."""
    now = _now(now)
    await db.ensure_partition("test_events", now)
    candidates, cutoffs = await _reap_candidates(db, now)
    failed: Counter[str] = Counter()
    closed = 0
    for candidate in candidates:
        if await _reap_batch(db, candidate["batch_id"], now, cutoffs, failed):
            closed += 1
    return {"failed": dict(failed), "closed": closed}


async def _reap_candidates(db: Database, now: datetime) -> tuple[list[dict], dict[str, datetime]]:
    """Open batches with a run to reap or none left to wait for, and the reaper's cutoffs."""
    async with db.connection() as conn:
        grace = timedelta(seconds=float(await setting(conn, "batch.start_grace_seconds")))
        stale = timedelta(seconds=float(await setting(conn, "batch.stale_seconds")))
        cutoffs = {"late": now - grace, "stale": now - stale, "ingest": now - UPLOAD_STALE * stale}
        cutoffs |= await reap_cutoffs(conn, now)
        candidates = await _rows(
            conn,
            "SELECT DISTINCT b.batch_id FROM test_batches b LEFT JOIN batch_runs br "
            "USING(batch_id) WHERE b.status IN ('scheduled','running') AND ("
            " br.batch_id IS NULL OR br.status IN ('done','failed','skipped','killed')"
            f" OR {REAPABLE}) ORDER BY b.batch_id",
            cutoffs,
        )
    return candidates, cutoffs


async def _reap_batch(
    db: Database,
    batch_id: int,
    now: datetime,
    cutoffs: dict[str, datetime],
    failed: Counter[str],
) -> bool:
    """Fail one batch's late and silent runs under its lock; whether the batch closed."""
    async with db.transaction() as conn:
        batch = await _row(
            conn,
            "SELECT status FROM test_batches WHERE batch_id=%s FOR UPDATE",
            (batch_id,),
        )
        if batch["status"] not in OPEN_BATCHES:
            return False
        reaped = await _rows(
            conn,
            "UPDATE batch_runs br SET status='failed',finished_at=%(now)s,"
            "stop_reason=coalesce(stop_reason,'error'),"
            "error=CASE WHEN br.started_at IS NULL THEN 'not_started' WHEN b.kind='browser' "
            "AND br.status='running' AND b.start_at<%(deadline)s THEN 'deadline' ELSE 'stale' END "
            "FROM test_batches b WHERE b.batch_id=br.batch_id AND br.batch_id=%(batch)s "
            f"AND {REAPABLE} "
            "RETURNING br.product_id,br.node_id,br.error,b.kind,b.target_id,"
            "(SELECT host FROM nodes WHERE node_id=br.node_id)",
            {"now": now, "batch": batch_id, **cutoffs},
        )
        for run in reaped:
            failed[run["error"]] += 1
            await _reaped_run(conn, batch_id, run, now)
        return await close_batch(conn, batch_id, now) not in OPEN_BATCHES


async def _reaped_run(
    conn: psycopg.AsyncConnection, batch_id: int, run: dict, now: datetime
) -> None:
    """census_finished and a minor alert for a run the reaper failed; a browser run abandoned
    as stuck alerts by its host (browser_hosts.abandoned)."""
    await run_finished(conn, batch_id, run["product_id"], now)
    if run["kind"] == BROWSER_KIND and run["error"] in REASONS:
        await abandoned(conn, batch_id, run, now)
        return
    await raise_alert(
        conn,
        "run_" + run["error"],
        f"node {run['node_id']} left a run {run['error'].replace('_', ' ')}",
        severity="minor",
        dedupe_key=f"run_{run['error']}:{run['node_id']}",
        data={"batch_id": batch_id, "node_id": run["node_id"]},
    )


async def online(conn: psycopg.AsyncConnection, node_id: int, values: dict[str, Any]) -> None:
    """Heartbeat or registration: the node is online, and its offline alert resolves."""
    # A retired node stays retired (node_offline.py): its droplet is going away.
    assignments = ["status=CASE WHEN n.status='retired' THEN 'retired' ELSE 'online' END"]
    assignments += ["last_heartbeat=now()"]
    assignments += [f"{key}=%({key})s" for key in values]
    previous = await _row(
        conn,
        # The self-join reads the row as it was before this update.
        "UPDATE nodes n SET " + ",".join(assignments) + " FROM nodes old "
        "WHERE n.node_id=%(node)s AND old.node_id=n.node_id RETURNING old.status",
        values | {"node": node_id},
    )
    if previous and previous["status"] == "offline":
        await conn.execute(
            "UPDATE alerts SET acknowledged_at=now() WHERE dedupe_key=%s "
            "AND acknowledged_at IS NULL",
            (f"node_offline:{node_id}",),
        )
