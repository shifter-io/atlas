"""The browser planner's slots mode (`[schedule] plan = "slots"`, browser.py): each settings
target's own interval, one batch per country per slot with a queued run for every network that
has a node in the browser region (moved out of browser.py, BROWSER-COVERAGE-1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from ..browser.contracts import BROWSER_KIND, BROWSER_REGION_ID
from ..browser.settings import BrowserSettings
from .batches import close_batch

GRACE = timedelta(seconds=60)  # a slot opened this late still uses its whole interval
LATE_END = timedelta(minutes=10)  # a late slot leaves this much of its end unplanned


def slot_starts(
    targets: list[tuple[str, int]], now: datetime, lead: timedelta
) -> list[tuple[str, datetime, datetime]]:
    """(target, slot, start_at) for each (target, every_minutes), in order, whose current slot
    may open now: on time, start_at = slot + lead + i x every / n; late (now more than GRACE
    after the slot), now + lead + i x (rest of the slot - LATE_END) / n, and none after that."""
    starts = []
    for i, (target, minutes) in enumerate(targets):
        every = timedelta(minutes=minutes)
        step = every.total_seconds()
        slot = datetime.fromtimestamp(now.timestamp() // step * step, UTC)
        late = now - slot > GRACE
        origin, span = (now, slot + every - LATE_END - now) if late else (slot, every)
        start = origin + lead + span * i / len(targets)
        if late and (span <= timedelta(0) or start > slot + every - LATE_END):
            continue
        starts.append((target, slot, start))
    return starts


async def _open(
    conn: psycopg.AsyncConnection, target: str, slot: datetime, start: datetime, countries
) -> list[int]:
    rows = await (
        await conn.execute(
            "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,"
            "start_at) SELECT %s,u.c,%s,%s,%s,%s FROM unnest(%s::text[]) u(c) ORDER BY u.c "
            "ON CONFLICT DO NOTHING RETURNING batch_id",
            (BROWSER_KIND, BROWSER_REGION_ID, target, slot, start, list(countries)),
        )
    ).fetchall()
    return [row["batch_id"] for row in rows]


async def create_runs(conn: psycopg.AsyncConnection, batch_ids: list[int]) -> int:
    """A queued run per network with a browser node in each batch; returns how many networks."""
    if batch_ids:
        await conn.execute(
            "INSERT INTO batch_runs(batch_id,product_id,node_id,status) "
            "SELECT b.id,p.product_id,n.node_id,'queued' FROM unnest(%s::bigint[]) b(id) "
            "CROSS JOIN nodes n JOIN products p ON p.provider=n.provider AND p.name=n.product "
            "WHERE n.region_id=%s ON CONFLICT DO NOTHING",
            (batch_ids, BROWSER_REGION_ID),
        )
    row = await (
        await conn.execute(
            "SELECT count(*) AS networks FROM nodes WHERE region_id=%s", (BROWSER_REGION_ID,)
        )
    ).fetchone()
    return row["networks"]


async def plan_slots(
    conn: psycopg.AsyncConnection, settings: BrowserSettings, now: datetime, missing: list[str]
) -> dict[str, Any]:
    lead = timedelta(seconds=settings.schedule.lead_seconds)
    targets = [(target.id, target.every_minutes) for target in settings.targets]
    opened, created = [], []
    for target, slot, start in slot_starts(targets, now, lead):
        if target in missing:  # its spread place stays free; the others keep theirs
            continue
        batch_ids = await _open(conn, target, slot, start, settings.schedule.countries)
        if batch_ids:
            opened.append(target)
            created.extend(batch_ids)
    networks = await create_runs(conn, created)
    if not networks:
        for batch_id in created:
            await close_batch(conn, batch_id, now)
    return {"opened": opened, "created": len(created), "networks": networks}
