"""How a node's run ends: finish_run and fail_run (nodework.py re-exports them), with the run
lookup and the counters that nodework.py's other transitions share. The lock order is batches.py's.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg

from .batches import (
    _moment,
    census_down,
    census_recovered,
    census_retry,
    close_batch,
    run_finished,
)
from .db import Database
from .sqlrows import _now, _row

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


async def _run(conn: psycopg.AsyncConnection, node: dict, batch_id: int) -> dict | None:
    return await _row(
        conn,
        "SELECT br.*,b.status AS batch_status,b.kind,b.target_id FROM batch_runs br "
        "JOIN test_batches b USING(batch_id) WHERE br.batch_id=%s AND br.node_id=%s",
        (batch_id, node["node_id"]),
    )


async def finish_run(
    db: Database,
    node: dict,
    batch_id: int,
    summary: dict[str, Any],
    run_key: int,
    now: datetime | None = None,
) -> str:
    """Link the ingested run and take its final counters from the summary; then the barrier.

    Its tape events carry the run's own times (TAPE-TIME-1): census_finished its reported
    finish (never after `now`), a census outage's down and recovered the outage's start and end.
    """
    now = _now(now)
    ended = min(_moment(summary.get("finished_at")) or now, now)
    completed = summary.get("status") == "completed"
    values = {
        "requests": summary.get("completed") or 0,
        "successes": summary.get("successes") or 0,
        "unique_ips": summary.get("unique_ips") or 0,
        **{key: summary.get(key) or 0 for key in COUNTERS[3:]},
    }
    for moment in {_moment(summary.get("started_at")) or now, now}:  # its events' months
        await db.ensure_partition("test_events", moment)
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
        await census_recovered(conn, batch_id, product_id, summary)
        await run_finished(conn, batch_id, product_id, ended)
        await census_down(conn, batch_id, product_id, summary)
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
