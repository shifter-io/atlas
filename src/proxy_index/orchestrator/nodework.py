"""Node-facing batch transitions: what a node's calls (api.py, browser_ingest.py) and the reaper
jobs (jobs.py) drive. Planning and the barrier are in batches.py; the lock order is the same.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from .batches import (
    OPEN_BATCHES,
    _start,
    census_down,
    census_recovered,
    census_retry,
    close_batch,
    run_finished,
)
from .db import Database, setting
from .events import EventRow, emit, raise_alert
from .sqlrows import _now, _row, _rows

COUNTERS = (
    "requests",
    "successes",
    "unique_ips",
    "challenges",
    "blocks",
    "timeouts",
    "proxy_errors",
    "bytes_up",
    "bytes_down",
)
# Nodes are handed runs this far ahead of start_at so they can wait on the wall clock.
ASSIGN_AHEAD = timedelta(minutes=5)
# An upload is being received or ingested (a large census takes minutes): allow it longer.
UPLOAD_STALE = 3


async def assignments(db: Database, node: dict, now: datetime | None = None) -> dict[str, Any]:
    """Hand a node its queued runs due within ASSIGN_AHEAD (marking them 'running'), and the
    batches it must stop. A run handed out but not yet started is handed out again, so a node
    that restarted while waiting for start_at does not lose it."""
    now = _now(now)
    async with db.transaction() as conn:
        min_sample = int(await setting(conn, "census.min_sample"))
        stall = float(await setting(conn, "census.stall_seconds"))
        stall_seconds = int(stall) if stall.is_integer() else stall
        pulse_timeout = int(await setting(conn, "pulse.timeout_ms"))
        rows = await _rows(
            conn,
            "SELECT b.batch_id,b.kind,b.country_code,b.target_id,b.start_at,r.slug AS region,"
            " br.product_id,p.provider,p.name,t.url,t.timeout_ms,t.census_concurrency,"
            " t.pulse_requests,t.pulse_concurrency "
            "FROM batch_runs br JOIN test_batches b USING(batch_id) "
            "JOIN products p ON p.product_id=br.product_id "
            "JOIN targets t ON t.target_id=b.target_id "
            "JOIN runner_regions r ON r.region_id=b.region_id "
            "WHERE br.node_id=%s AND b.status IN ('scheduled','running') "
            " AND (br.status='queued' OR (br.status='running' AND br.started_at IS NULL)) "
            " AND b.start_at<=%s ORDER BY b.start_at,b.batch_id FOR UPDATE OF br SKIP LOCKED",
            (node["node_id"], now + ASSIGN_AHEAD),
        )
        if rows:
            await conn.execute(
                "UPDATE batch_runs SET status='running' WHERE node_id=%s AND batch_id=ANY(%s) "
                "AND status='queued'",
                (node["node_id"], [row["batch_id"] for row in rows]),
            )
        kill = await _rows(
            conn,
            "SELECT br.batch_id FROM batch_runs br JOIN test_batches b USING(batch_id) "
            "WHERE br.node_id=%s AND (br.status IN ('killed','skipped') "
            " OR b.status IN ('killed','skipped')) "
            "AND coalesce(br.finished_at,b.finished_at,%s)>%s ORDER BY br.batch_id",
            (node["node_id"], now, now - timedelta(minutes=15)),
        )
    work = []
    for row in rows:
        census = row["kind"] == "census"
        # The load wave: census concurrency, one request per connection, counted like a pulse.
        load = row["kind"] == "load"
        work.append(
            {
                "batch_id": row["batch_id"],
                "kind": row["kind"],
                "product_id": row["product_id"],
                "provider": row["provider"],
                "product": row["name"],
                "country_code": row["country_code"].strip(),
                "region": row["region"],
                "target_id": row["target_id"],
                "target_url": row["url"] or None,
                "timeout_ms": pulse_timeout if row["kind"] == "pulse" else row["timeout_ms"],
                "concurrency": row["census_concurrency"]
                if census or load
                else row["pulse_concurrency"],
                "stop_rule": "unique_ratio" if census else "count",
                "requests": None
                if census
                else row["census_concurrency"]
                if load
                else row["pulse_requests"],
                "min_sample": min_sample if census else None,
                "stall_seconds": stall_seconds if census else None,
                "start_at": row["start_at"].astimezone(UTC).isoformat(),
            }
        )
    return {"assignments": work, "kill": [row["batch_id"] for row in kill]}


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


async def _run(conn: psycopg.AsyncConnection, node: dict, batch_id: int) -> dict | None:
    return await _row(
        conn,
        "SELECT br.*,b.status AS batch_status,b.kind,b.target_id FROM batch_runs br "
        "JOIN test_batches b USING(batch_id) WHERE br.batch_id=%s AND br.node_id=%s",
        (batch_id, node["node_id"]),
    )


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
        updated = await _row(
            conn,
            "UPDATE batch_runs SET last_progress_at=%(now)s,"
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


async def finish_run(
    db: Database,
    node: dict,
    batch_id: int,
    summary: dict[str, Any],
    run_key: int,
    now: datetime | None = None,
) -> str:
    """Link the ingested run and take its final counters from the summary; then the barrier."""
    now = _now(now)
    completed = summary.get("status") == "completed"
    values = {
        "requests": summary.get("completed") or 0,
        "successes": summary.get("successes") or 0,
        "unique_ips": summary.get("unique_ips") or 0,
        **{key: summary.get(key) or 0 for key in COUNTERS[3:]},
    }
    await db.ensure_partition("test_events", now)
    async with db.transaction() as conn:
        await _row(conn, "SELECT 1 FROM test_batches WHERE batch_id=%s FOR UPDATE", (batch_id,))
        updated = await _row(
            conn,
            "UPDATE batch_runs SET status=%(status)s,run_key=%(run_key)s,finished_at=%(now)s,"
            "last_progress_at=%(now)s,started_at=coalesce(started_at,%(started)s),"
            "stop_reason=%(stop_reason)s,error=%(error)s,"
            + ",".join(f"{key}=%({key})s" for key in COUNTERS)
            + " WHERE batch_id=%(batch)s AND node_id=%(node)s "
            "AND status IN ('running','uploading') RETURNING status,product_id",
            values
            | {
                "status": "done" if completed else "failed",
                "run_key": run_key,
                "now": now,
                "started": now,
                "stop_reason": summary.get("stop_reason"),
                "error": None if completed else f"run {summary.get('status')}",
                "batch": batch_id,
                "node": node["node_id"],
            },
        )
        if updated is None:
            run = await _run(conn, node, batch_id)
            return run["status"] if run else "missing"
        product_id = updated["product_id"]
        await census_recovered(conn, batch_id, product_id, summary, now)
        await run_finished(conn, batch_id, product_id, now)
        await census_down(conn, batch_id, product_id, summary, now)
        await close_batch(conn, batch_id, now)
        await census_retry(conn, batch_id, product_id, now)
        return updated["status"]


async def fail_run(
    db: Database, node: dict, batch_id: int, error: str, now: datetime | None = None
) -> str | None:
    now = _now(now)
    await db.ensure_partition("test_events", now)
    async with db.transaction() as conn:
        await _row(conn, "SELECT 1 FROM test_batches WHERE batch_id=%s FOR UPDATE", (batch_id,))
        run = await _row(
            conn,
            "UPDATE batch_runs SET status='failed',error=%s,finished_at=%s,"
            "stop_reason=coalesce(stop_reason,'error') WHERE batch_id=%s AND node_id=%s "
            "AND status NOT IN ('done','failed','skipped','killed') RETURNING product_id",
            (error[:500], now, batch_id, node["node_id"]),
        )
        if run is None:
            existing = await _run(conn, node, batch_id)
            return existing["status"] if existing else None
        await run_finished(conn, batch_id, run["product_id"], now)
        await close_batch(conn, batch_id, now)
        return "failed"


# --- reaper ------------------------------------------------------------------------------


async def reap(db: Database, now: datetime | None = None) -> dict[str, Any]:
    """Fail runs that never started or went silent, then close what that completes."""
    now = _now(now)
    await db.ensure_partition("test_events", now)
    async with db.connection() as conn:
        grace = timedelta(seconds=float(await setting(conn, "batch.start_grace_seconds")))
        stale = timedelta(seconds=float(await setting(conn, "batch.stale_seconds")))
        candidates = await _rows(
            conn,
            "SELECT DISTINCT b.batch_id FROM test_batches b LEFT JOIN batch_runs br "
            "USING(batch_id) WHERE b.status IN ('scheduled','running') AND ("
            " br.batch_id IS NULL OR br.status IN ('done','failed','skipped','killed')"
            " OR (br.started_at IS NULL AND b.start_at<%(late)s)"
            " OR (br.started_at IS NOT NULL AND br.status='running'"
            "  AND coalesce(br.last_progress_at,br.started_at)<%(stale)s)"
            " OR (br.status='uploading' AND br.last_progress_at<%(ingest)s)) ORDER BY b.batch_id",
            {"late": now - grace, "stale": now - stale, "ingest": now - UPLOAD_STALE * stale},
        )
    failed: Counter[str] = Counter()
    closed = 0
    for candidate in candidates:
        batch_id = candidate["batch_id"]
        async with db.transaction() as conn:
            batch = await _row(
                conn,
                "SELECT status FROM test_batches WHERE batch_id=%s FOR UPDATE",
                (batch_id,),
            )
            if batch["status"] not in OPEN_BATCHES:
                continue
            reaped = await _rows(
                conn,
                "UPDATE batch_runs br SET status='failed',finished_at=%(now)s,"
                "stop_reason=coalesce(stop_reason,'error'),"
                "error=CASE WHEN br.started_at IS NULL THEN 'not_started' ELSE 'stale' END "
                "FROM test_batches b WHERE b.batch_id=br.batch_id AND br.batch_id=%(batch)s AND ("
                " (br.started_at IS NULL AND br.status IN ('queued','running')"
                "  AND b.start_at<%(late)s)"
                " OR (br.started_at IS NOT NULL AND br.status='running'"
                "  AND coalesce(br.last_progress_at,br.started_at)<%(stale)s)"
                " OR (br.status='uploading' AND br.last_progress_at<%(ingest)s)) "
                "RETURNING br.product_id,br.node_id,br.error",
                {
                    "now": now,
                    "batch": batch_id,
                    "late": now - grace,
                    "stale": now - stale,
                    "ingest": now - UPLOAD_STALE * stale,
                },
            )
            for run in reaped:
                failed[run["error"]] += 1
                await run_finished(conn, batch_id, run["product_id"], now)
                await raise_alert(
                    conn,
                    "run_" + run["error"],
                    f"node {run['node_id']} left a run {run['error'].replace('_', ' ')}",
                    severity="minor",
                    dedupe_key=f"run_{run['error']}:{run['node_id']}",
                    data={"batch_id": batch_id, "node_id": run["node_id"]},
                )
            if await close_batch(conn, batch_id, now) not in OPEN_BATCHES:
                closed += 1
    return {"failed": dict(failed), "closed": closed}


async def mark_offline(db: Database, now: datetime | None = None) -> dict[str, Any]:
    """Nodes without a heartbeat for node.offline_seconds go offline: alert and tape event."""
    now = _now(now)
    await db.ensure_partition("test_events", now)
    async with db.transaction() as conn:
        limit = timedelta(seconds=float(await setting(conn, "node.offline_seconds")))
        gone = await _rows(
            conn,
            "UPDATE nodes n SET status='offline' FROM runner_regions r "
            "WHERE r.region_id=n.region_id AND n.status<>'offline' "
            "AND (n.last_heartbeat IS NULL OR n.last_heartbeat<%s) "
            "RETURNING n.node_id,n.region_id,r.slug AS region,n.provider,n.product,n.host,"
            "n.last_heartbeat,(SELECT product_id FROM products p WHERE p.provider=n.provider "
            "AND p.name=n.product) AS product_id",
            (now - limit,),
        )
        for node in gone:
            data = {
                "region": node["region"],
                "provider": node["provider"],
                "product": f"{node['provider']}/{node['product']}",
                "node_id": node["node_id"],
                "host": node["host"],
                "last_heartbeat": node["last_heartbeat"].astimezone(UTC).isoformat()
                if node["last_heartbeat"]
                else None,
            }
            await raise_alert(
                conn,
                "node_offline",
                f"node {node['node_id']} ({data['product']} in {node['region']}) is offline",
                severity="major",
                dedupe_key=f"node_offline:{node['node_id']}",
                data=data,
            )
            await emit(
                conn,
                EventRow(
                    kind="node_offline",
                    at=now,
                    product_id=node["product_id"],
                    region_id=node["region_id"],
                    signal=-1,
                    severity="major",
                    data=data,
                ),
            )
    return {"offline": [node["node_id"] for node in gone]}


async def online(conn: psycopg.AsyncConnection, node_id: int, values: dict[str, Any]) -> None:
    """Heartbeat or registration: the node is online, and its offline alert resolves."""
    assignments = ["status='online'", "last_heartbeat=now()"]
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
