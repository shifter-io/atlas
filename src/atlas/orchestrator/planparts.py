"""The planning parts the batch planners share: the participating products (`TAKING`,
`participants`), one batch_runs row per product with the no_node gaps and their alerts
(`_create_runs`, `_report_no_node`), and the aligned census and pulse slots (`census_every`,
`interval_slots`, `next_slot`). batches.py imports them back. Of the batch modules it imports
only sqlrows.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from ..browser.contracts import BROWSER_REGION_ID
from .events import EventRow, emit, raise_alert
from .sqlrows import _rows

TAKING = (
    "SELECT DISTINCT p.product_id,p.provider,p.name FROM nodes n JOIN products p ON "
    f"p.provider=n.provider AND p.name=n.product AND n.region_id<>{BROWSER_REGION_ID}"
)


async def participants(conn: psycopg.AsyncConnection) -> list[dict]:
    """Products with at least one registered node, in any region but the browser one."""
    return await _rows(conn, TAKING + " ORDER BY p.product_id")


async def _create_runs(
    conn: psycopg.AsyncConnection, batch_ids: list[int], now: datetime
) -> list[dict]:
    """One batch_runs row per participating product; returns the no_node gaps per region."""
    if not batch_ids:
        return []
    await conn.execute(
        "WITH taking AS (" + TAKING + ") "
        "INSERT INTO batch_runs(batch_id,product_id,node_id,status,finished_at,stop_reason,error) "
        "SELECT b.batch_id,t.product_id,n.node_id,"
        " CASE WHEN n.node_id IS NULL THEN 'failed' ELSE 'queued' END,"
        " CASE WHEN n.node_id IS NULL THEN %(now)s END,"
        " CASE WHEN n.node_id IS NULL THEN 'no_node' END,"
        " CASE WHEN n.node_id IS NULL THEN 'no_node' END "
        "FROM test_batches b CROSS JOIN taking t LEFT JOIN nodes n ON n.region_id=b.region_id "
        " AND n.provider=t.provider AND n.product=t.name "
        "WHERE b.batch_id=ANY(%(ids)s) ON CONFLICT DO NOTHING",
        {"ids": batch_ids, "now": now},
    )
    return await _rows(
        conn,
        "SELECT br.product_id,p.provider,p.name,b.region_id,r.slug AS region,b.kind,"
        " count(*) AS batches,array_agg(b.country_code ORDER BY b.country_code) AS countries "
        "FROM batch_runs br JOIN test_batches b USING(batch_id) JOIN products p USING(product_id) "
        "JOIN runner_regions r ON r.region_id=b.region_id "
        "WHERE br.batch_id=ANY(%s) AND br.error='no_node' "
        "GROUP BY br.product_id,p.provider,p.name,b.region_id,r.slug,b.kind",
        (batch_ids,),
    )


async def _report_no_node(conn: psycopg.AsyncConnection, gaps: list[dict], now: datetime) -> int:
    events = 0
    for gap in gaps:
        label = f"{gap['provider']}/{gap['name']}"
        data = {
            "region": gap["region"],
            "provider": gap["provider"],
            "product": label,
            "kind": gap["kind"],
            "batches": gap["batches"],
            "countries": [c.strip() for c in gap["countries"]][:50],
        }
        opened = await raise_alert(
            conn,
            "no_node",
            f"{label} has no node in {gap['region']}; its {gap['kind']} runs there fail",
            severity="major",
            dedupe_key=f"no_node:{gap['product_id']}:{gap['region_id']}",
            data=data,
        )
        if opened:
            await emit(
                conn,
                EventRow(
                    kind="no_node",
                    at=now,
                    product_id=gap["product_id"],
                    region_id=gap["region_id"],
                    signal=-1,
                    severity="major",
                    data=data,
                ),
            )
            events += 1
    return events


def census_every(value: Any) -> timedelta | None:
    """The census.every_minutes setting as an interval; None (unset) means the daily slot."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 1:
        raise ValueError("census.every_minutes must be a positive number of minutes or null")
    return timedelta(minutes=value)


def interval_slots(every: timedelta, lo: datetime, hi: datetime) -> list[datetime]:
    """The slots aligned to multiples of `every` since the epoch in [lo, hi)."""
    step = every.total_seconds()
    k = -(-lo.timestamp() // step)  # ceil
    slots = []
    while k * step < hi.timestamp():
        slots.append(datetime.fromtimestamp(k * step, UTC))
        k += 1
    return slots


def next_slot(every: timedelta, now: datetime) -> datetime:
    """The first aligned slot after `now`."""
    step = every.total_seconds()
    return datetime.fromtimestamp((now.timestamp() // step + 1) * step, UTC)
