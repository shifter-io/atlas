"""Queue entries whose run the reaper failed as never started (BROWSER-COVERAGE-1, #1392).

The queue visits each (network, target, country) once a period, so a run lost on our side waits
a whole period for its next turn: with a 22-hour period, 44 hours or more between two counted
runs, past coverage.browser_hours. A run its node never started (the reaper's 'not_started': the
host was cut off from the manager, or full) is therefore run again in the same period: once its
node is online again, the planner (browser.plan_queue) catches its entry up like one it missed
itself, at the host's next free position, if a run starting there still ends before the entry's
next start; else it stays failed. Its batch is reopened (status 'scheduled', start_at the new
position, its run 'queued' again), as a late upload reopens a reaped run's batch
(browser_ingest), so scheduled_for stays the entry's own start and the catch-up holds its
position like any other.

A run that did start is not run again here: one the reaper failed as 'stale' or past its
deadline kept its node's data, which its node may still upload (browser_ingest.REAPED).
"""

from __future__ import annotations

from datetime import datetime

import psycopg

from ..browser.contracts import BROWSER_KIND
from .sqlrows import _rows

# The failed batches since a time whose only run its online node never started and posted no
# progress for: (target, country, scheduled_for) -> batch_id.
LOST_SQL = (
    "SELECT b.batch_id,b.target_id,b.country_code,b.scheduled_for FROM test_batches b "
    "JOIN batch_runs br USING(batch_id) JOIN nodes n USING(node_id) "
    "WHERE b.kind=%s AND b.scheduled_for>=%s AND b.status='failed' AND br.status='failed' "
    "AND br.error='not_started' AND br.started_at IS NULL AND br.last_progress_at IS NULL "
    "AND br.run_key IS NULL AND n.status='online' "
    "AND NOT EXISTS (SELECT FROM batch_runs o WHERE o.batch_id=b.batch_id "
    "AND o.product_id<>br.product_id)"
)
REOPEN_BATCH = (
    "UPDATE test_batches SET status='scheduled',start_at=%s,started_at=NULL,finished_at=NULL "
    "WHERE batch_id=%s AND status='failed'"
)
REOPEN_RUN = (
    "UPDATE batch_runs SET status='queued',error=NULL,stop_reason=NULL,finished_at=NULL "
    "WHERE batch_id=%s AND status='failed' AND error='not_started'"
)


async def lost(conn: psycopg.AsyncConnection, since: datetime) -> dict[tuple, int]:
    """The batches to run again (LOST_SQL) scheduled since `since`."""
    rows = await _rows(conn, LOST_SQL, (BROWSER_KIND, since))
    return {
        (row["target_id"], row["country_code"], row["scheduled_for"]): row["batch_id"]
        for row in rows
    }


async def reopen(conn: psycopg.AsyncConnection, batch_id: int, start: datetime) -> None:
    """Run the batch again from `start`: scheduled, its run queued for its node."""
    await conn.execute(REOPEN_BATCH, (start, batch_id))
    await conn.execute(REOPEN_RUN, (batch_id,))
