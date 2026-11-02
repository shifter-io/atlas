"""Hourly release of the estimated hours, with one estimate_hour_released event per network hour.

An estimated hour is released once its UTC clock hour has ended: from then on the calculator
windows include it. Every due hour is released in (day, hour) order in one transaction with its
events, so an hour is never released without its event, nor announced twice. After a gap (the
orchestrator was down) the missed hours are released together, oldest first.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..orchestrator import events
from ..orchestrator.db import Database

RELEASE = """WITH due AS (
 UPDATE estimated_hours SET released_at=%(now)s
 WHERE released_at IS NULL
  AND (day + make_interval(hours => hour + 1)) AT TIME ZONE 'UTC' <= %(now)s
 RETURNING product_id,day,hour,formula_version,unique_ips)
SELECT product_id,day,hour,max(formula_version) AS formula_version,
 round(sum(unique_ips))::bigint AS unique_ips
FROM due GROUP BY product_id,day,hour ORDER BY day,hour,product_id"""


async def release_due(db: Database, now: datetime | None = None) -> list[events.HourEstimate]:
    """Release every estimated hour that has ended by `now`; returns the announced hours."""
    now = now or datetime.now(UTC)
    await db.ensure_partition("test_events", now)
    async with db.transaction() as conn:
        rows = await (await conn.execute(RELEASE, {"now": now})).fetchall()
        released = [
            events.HourEstimate(
                product_id=row["product_id"],
                day=row["day"],
                hour=row["hour"],
                formula_version=str(row["formula_version"]),
                unique_ips=row["unique_ips"],
            )
            for row in rows
        ]
        for estimate in released:
            await events.emit_estimate_hour_released(conn, now, estimate)
    return released
