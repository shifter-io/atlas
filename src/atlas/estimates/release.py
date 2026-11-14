"""Hourly release of the estimated hours, with one estimate_hour_released event per network hour.

The event stands for all of the hour's countries (their count is in its payload), so a full hour
of 18 networks x 40 countries writes 18 tape events, not 720.

An estimated hour is released once its UTC clock hour has ended: from then on the calculator
windows include it. Every due hour is released in (day, hour) order in one transaction with its
events, so an hour is never released without its event, nor announced twice. After a gap (the
orchestrator was down) the missed hours are released together, oldest first. Each event is at
its hour's end, the moment the hour became releasable, never at the release (TAPE-TIME-1).
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from ..orchestrator import events
from ..orchestrator.db import Database

# An estimated hour is due once its UTC clock hour has ended by %(now)s.
DUE = (
    "released_at IS NULL AND (day + make_interval(hours => hour + 1)) AT TIME ZONE 'UTC' <= %(now)s"
)
RELEASE = (
    "WITH due AS (UPDATE estimated_hours SET released_at=%(now)s WHERE " + DUE + " "
    "RETURNING product_id,day,hour,formula_version,unique_ips) "
    "SELECT product_id,day,hour,max(formula_version) AS formula_version,"
    "round(sum(unique_ips))::bigint AS unique_ips,count(*)::int AS countries "
    "FROM due GROUP BY product_id,day,hour ORDER BY day,hour,product_id"
)
# The months the due hours end in: their events' partitions.
MONTHS = (
    "SELECT DISTINCT date_trunc('month',day + make_interval(hours => hour + 1)) "
    "AT TIME ZONE 'UTC' AS month FROM estimated_hours WHERE " + DUE
)


async def release_due(db: Database, now: datetime | None = None) -> list[events.HourEstimate]:
    """Release every estimated hour that has ended by `now`; returns the announced hours."""
    now = now or datetime.now(UTC)
    async with db.connection() as conn:
        months = await (await conn.execute(MONTHS, {"now": now})).fetchall()
    for moment in [now, *(row["month"] for row in months)]:  # before the inserting transaction
        await db.ensure_partition("test_events", moment)
    async with db.transaction() as conn:
        rows = await (await conn.execute(RELEASE, {"now": now})).fetchall()
        released = [
            events.HourEstimate(
                product_id=row["product_id"],
                day=row["day"],
                hour=row["hour"],
                formula_version=str(row["formula_version"]),
                unique_ips=row["unique_ips"],
                countries=row["countries"],
            )
            for row in rows
        ]
        for estimate in released:
            await events.emit_estimate_hour_released(conn, hour_end(estimate), estimate)
    return released


def hour_end(estimate: events.HourEstimate) -> datetime:
    """The end of the estimate's UTC clock hour."""
    return datetime.combine(estimate.day, time(estimate.hour), UTC) + timedelta(hours=1)
