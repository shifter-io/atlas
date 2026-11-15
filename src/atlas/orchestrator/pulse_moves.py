"""PULSE-STATE-1 (2/2): a pulse node that stops mid-hour, and the runs a removed droplet leaves.

- stalled(): the hour's nodes (not retired) with a run due `pulse.stall_minutes` (default 3) ago
  still queued or running, and no heartbeat, or no run of the hour started, progressed or
  finished, in that time; with the moment it was last seen working.
- move_runs(): such a node's runs of the hour not uploaded yet go to another node of the same
  network (another region's droplet, or its own cell's replacement: the same node row), queued
  afresh. A batch due later keeps its start; one whose start passed starts again back to back
  from now + pulse.lead_seconds, half a pulse.step_seconds apart (its two targets alternate),
  none after LAST_START: those fail 'node_lost'. The stalled node's late upload then finds no run
  of its own (404) and its progress is told to stop. A pulse batch is shared by the networks of
  its region; moving one network's run never touches the others' (done ones keep their data).
- fail_removed(): once the hour's droplets are removed, their runs still queued or running fail
  'node_removed' and their batches close, so no run of a gone droplet stays running (an
  uploading one is on the manager already and is stored).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import psycopg

from .batches import OPEN_BATCHES, PLAN_LOCK, close_batch, run_finished
from .db import Database, setting
from .pulse_hour import LAST_START
from .sqlrows import _now, _row, _rows

STALLED_SQL = (
    "WITH h AS (SELECT br.node_id,br.status,b.start_at,"
    "greatest(br.started_at,br.last_progress_at,br.finished_at) AS seen FROM batch_runs br "
    "JOIN test_batches b USING(batch_id) WHERE b.kind='pulse' AND b.scheduled_for=%(hour)s) "
    "SELECT n.node_id,n.provider,n.product,r.slug AS region,max(h.seen) AS last_seen FROM h "
    "JOIN nodes n USING(node_id) JOIN runner_regions r ON r.region_id=n.region_id "
    "WHERE n.status<>'retired' GROUP BY n.node_id,r.slug "
    "HAVING bool_or(h.status IN ('queued','running') AND h.start_at<%(cut)s) "
    "AND (n.last_heartbeat IS NULL OR n.last_heartbeat<%(cut)s "
    "OR coalesce(max(h.seen),'-infinity')<%(cut)s) ORDER BY n.node_id"
)
# The node's runs of the hour not uploaded yet, in their order.
LEFT_SQL = (
    "SELECT br.batch_id,br.product_id,b.start_at FROM batch_runs br JOIN test_batches b "
    "USING(batch_id) WHERE br.node_id=%s AND b.kind='pulse' AND b.scheduled_for=%s "
    "AND b.status=ANY(%s) AND br.status IN ('queued','running') "
    "ORDER BY b.start_at,b.batch_id FOR UPDATE OF br"
)
NODE_SQL = (
    "SELECT n.node_id FROM nodes n JOIN runner_regions r ON r.region_id=n.region_id "
    "WHERE n.provider=%s AND n.product=%s AND r.slug=%s"
)
REQUEUE = (
    "status='queued',node_id=%(to)s,started_at=NULL,finished_at=NULL,last_progress_at=NULL,"
    "requests=0,successes=0,unique_ips=0,challenges=0,blocks=0,timeouts=0,proxy_errors=0,"
    "bytes_up=0,bytes_down=0,stop_reason=NULL,error=NULL"
)


async def stalled(db: Database, hour: datetime, now: datetime | None = None) -> list[dict]:
    """The hour's stalled nodes: node_id, provider, product, region and last_seen."""
    now = _now(now)
    async with db.connection() as conn:
        minutes = float(await setting(conn, "pulse.stall_minutes"))
        cut = now - timedelta(minutes=minutes)
        return await _rows(conn, STALLED_SQL, {"hour": hour, "cut": cut})


def _starts(rows: list[dict], begin: datetime, half: timedelta, last: datetime):
    """New starts for the runs due before `begin`, back to back; None past `last` (lost)."""
    starts, late = {}, 0
    for row in rows:
        if row["start_at"] >= begin:
            continue
        start = begin + half * late
        late += 1
        starts[row["batch_id"]] = start if start <= last else None
    return starts


async def move_runs(
    db: Database, hour: datetime, node_id: int, to: tuple[str, str, str], now=None
) -> dict:
    """Move a stalled node's runs of the hour to the node `to` (provider, product, region);
    returns how many moved and how many were lost (no time left before LAST_START)."""
    now = _now(now)
    async with db.transaction() as conn:
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (PLAN_LOCK,))
        lead = timedelta(seconds=float(await setting(conn, "pulse.lead_seconds")))
        step = timedelta(seconds=float(await setting(conn, "pulse.step_seconds")))
        target = await _row(conn, NODE_SQL, to)
        rows = await _rows(conn, LEFT_SQL, (node_id, hour, list(OPEN_BATCHES)))
        starts = _starts(rows, max(now, hour) + lead, step / 2, hour + LAST_START)
        lost = [r for r in rows if r["batch_id"] in starts and starts[r["batch_id"]] is None]
        moved = [r["batch_id"] for r in rows if r not in lost]
        if target is None:
            lost, moved = rows, []
        timed = [(b, s) for b, s in starts.items() if s is not None and b in moved]
        if timed:
            await conn.execute(
                "UPDATE test_batches b SET start_at=v.start FROM unnest(%s::bigint[],"
                "%s::timestamptz[]) v(id,start) WHERE b.batch_id=v.id",
                ([b for b, _ in timed], [s for _, s in timed]),
            )
        if moved:
            await conn.execute(
                f"UPDATE batch_runs SET {REQUEUE} WHERE node_id=%(node)s AND batch_id=ANY(%(ids)s)",
                {"to": target["node_id"], "node": node_id, "ids": moved},
            )
        await _fail(conn, [(r["batch_id"], r["product_id"]) for r in lost], "node_lost", now)
    return {"moved": len(moved), "lost": len(lost)}


async def _fail(conn: psycopg.AsyncConnection, runs: list, error: str, now: datetime) -> None:
    """Fail the runs (batch_id, product_id), then close their batches once all runs ended."""
    for batch_id, product_id in runs:
        await conn.execute(
            "UPDATE batch_runs SET status='failed',finished_at=%s,"
            "stop_reason=coalesce(stop_reason,'error'),error=%s "
            "WHERE batch_id=%s AND product_id=%s",
            (now, error, batch_id, product_id),
        )
        await run_finished(conn, batch_id, product_id, now)
    for batch_id in sorted({batch_id for batch_id, _ in runs}):
        await close_batch(conn, batch_id, now)


async def fail_removed(
    db: Database, hour: datetime, keys: list[tuple[str, str, str]], now=None
) -> int:
    """Fail the runs of the hour still queued or running on the nodes `keys` (provider, product,
    region) whose droplets were removed; returns how many."""
    now = _now(now)
    if not keys:
        return 0
    async with db.transaction() as conn:
        rows = await _rows(
            conn,
            "SELECT br.batch_id,br.product_id FROM batch_runs br JOIN test_batches b "
            "USING(batch_id) JOIN nodes n ON n.node_id=br.node_id JOIN runner_regions r "
            "ON r.region_id=n.region_id JOIN unnest(%s::text[],%s::text[],%s::text[]) "
            "w(provider,product,region) ON w.provider=n.provider AND w.product=n.product "
            "AND w.region=r.slug WHERE b.kind='pulse' AND b.scheduled_for=%s "
            "AND b.status=ANY(%s) AND br.status IN ('queued','running') "
            "ORDER BY br.batch_id FOR UPDATE OF br",
            tuple([k[i] for k in keys] for i in range(3)) + (hour, list(OPEN_BATCHES)),
        )
        await _fail(conn, [(r["batch_id"], r["product_id"]) for r in rows], "node_removed", now)
    return len(rows)
