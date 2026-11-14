"""PULSE-ONDEMAND-1: one hour's pulses for the on-demand droplets armed for it.

With the setting `pulse.on_demand` the minute planners plan nothing (batches.py); the hourly
pulse job creates one droplet per network and region (daily.pulses), arms each as an `atlas
node` and then calls `open_hour` with the cells it armed. For every pulse-enabled target it
inserts one pulse batch per country of those cells (scheduled_for = the hour, so the target's
pulse_interval_minutes must be 60; a target with another interval is skipped and reported) and
one batch_runs row per armed product whose cell holds the country. A product without a droplet
in the hour gets no run, so no `no_node` failure or alert.

A region's starts are spread over `pulse.pack_minutes` (40) from now (never before the hour) +
pulse.lead_seconds, the targets half a step apart, with no 60-second floor: a pulse of 100
requests at 25 takes about 10 to 15 s. The window is cut so the last start is at least TAIL
before the hour ends; an hour with no time left opens nothing. A second call for the same hour
adds only what is new: the batches of countries not yet opened (UNIQUE(kind, country, target,
scheduled_for)) and the runs of newly armed cells in the hour's batches still to start (PRIMARY
KEY(batch_id, product_id)).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from .batches import PLAN_LOCK, _close_unmanned
from .db import Database, setting
from .pulse_slots import insert_pulses, spread
from .sqlrows import _now, _rows

HOUR = timedelta(hours=1)
# The last pulse of the hour starts at least this before its end: by HH:45, five minutes
# before the hourly job stops waiting (campaign.pulse_ondemand.DONE_BY, HH:50).
TAIL = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class Armed:
    """A droplet armed for the hour: its network's provider and product, region and countries."""

    provider: str
    product: str
    region: str
    countries: tuple[str, ...]


def window(hour: datetime, begin: datetime, pack: timedelta) -> timedelta:
    """How long a region's starts may spread from `begin`: the pack, cut to the hour's rest."""
    return max(timedelta(0), min(pack, hour + HOUR - TAIL - begin))


async def _countries(conn: psycopg.AsyncConnection, cells: list[Armed]) -> list[tuple[str, int]]:
    """The enabled (country, region_id) pairs of the cells, in enabled regions."""
    wanted = sorted({(c, cell.region) for cell in cells for c in cell.countries})
    rows = await _rows(
        conn,
        "SELECT c.country_code,c.region_id FROM countries c JOIN runner_regions r "
        "ON r.region_id=c.region_id AND r.enabled JOIN unnest(%s::text[],%s::text[]) w(c,r) "
        "ON w.c=c.country_code AND w.r=r.slug WHERE c.enabled ORDER BY 1",
        ([w[0] for w in wanted], [w[1] for w in wanted]),
    )
    return [(row["country_code"].strip(), row["region_id"]) for row in rows]


async def _hour_batches(
    conn: psycopg.AsyncConnection, hour: datetime, targets: list, now: datetime
) -> list:
    """The hour's pulse batches of its hourly targets still to start, an earlier call's too: a
    closed batch or one already due takes no new run (it would never be claimed, or fail)."""
    rows = await _rows(
        conn,
        "SELECT batch_id FROM test_batches WHERE kind='pulse' AND scheduled_for=%s "
        "AND target_id=ANY(%s) AND status='scheduled' AND start_at>%s ORDER BY batch_id",
        (hour, targets, now),
    )
    return [row["batch_id"] for row in rows]


async def _runs(conn: psycopg.AsyncConnection, ids: list[int], cells: list[Armed]) -> int:
    """One queued run per batch and armed product whose cell holds the batch's country."""
    pairs = sorted({(cell.provider, cell.product, c) for cell in cells for c in cell.countries})
    cursor = await conn.execute(
        "INSERT INTO batch_runs(batch_id,product_id,node_id,status) "
        "SELECT b.batch_id,p.product_id,n.node_id,'queued' FROM test_batches b "
        "JOIN unnest(%s::text[],%s::text[],%s::text[]) w(provider,product,country) "
        "ON w.country=b.country_code JOIN products p ON p.provider=w.provider "
        "AND p.name=w.product JOIN nodes n ON n.region_id=b.region_id "
        "AND n.provider=w.provider AND n.product=w.product "
        "WHERE b.batch_id=ANY(%s) ON CONFLICT DO NOTHING",
        ([p[0] for p in pairs], [p[1] for p in pairs], [p[2] for p in pairs], ids),
    )
    return cursor.rowcount


async def open_hour(
    db: Database, hour: datetime, cells: list[Armed], now: datetime | None = None
) -> dict[str, Any]:
    """Open the hour's pulse batches and runs for the armed cells; returns what it did."""
    now, hour = _now(now), hour.astimezone(UTC)
    if hour.minute or hour.second or hour.microsecond:
        raise ValueError("the pulse hour must be a whole UTC hour")
    async with db.transaction() as conn:
        if not await setting(conn, "pulse.on_demand"):
            return {"targets": [], "created": 0, "skipped": "pulse.on_demand is off"}
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (PLAN_LOCK,))
        lead = timedelta(seconds=float(await setting(conn, "pulse.lead_seconds")))
        pack = timedelta(minutes=float(await setting(conn, "pulse.pack_minutes")))
        begin = max(now, hour) + lead
        span = window(hour, begin, pack)
        if span <= timedelta(0):
            return {"targets": [], "created": 0, "skipped": "no time left in the hour"}
        targets = await _rows(
            conn,
            "SELECT target_id,pulse_interval_minutes FROM targets WHERE pulse_enabled "
            "ORDER BY sort_order,target_id",
        )
        hourly = [t for t in targets if t["pulse_interval_minutes"] == 60]
        countries = await _countries(conn, cells)
        created, opened = [], []
        for index, target in enumerate(hourly):
            starts = spread(countries, begin, span, index / len(hourly))
            ids = await insert_pulses(conn, target["target_id"], hour, starts)
            if ids:
                created.extend(ids)
                opened.append(target["target_id"])
        hour_ids = await _hour_batches(conn, hour, [t["target_id"] for t in hourly], now)
        runs = await _runs(conn, hour_ids, cells) if hour_ids else 0
        if created:
            await _close_unmanned(conn, created, now)
    return {
        "targets": opened,
        "created": len(created),
        "runs": runs,
        "not_hourly": [t["target_id"] for t in targets if t not in hourly],
    }


async def retire_nodes(db: Database, cells: list[Armed]) -> int:
    """Retire the nodes of the hour's cells before their droplets go (node_offline.py): the
    offline job then never alerts on them. Returns how many rows it retired."""
    keys = sorted({(cell.provider, cell.product, cell.region) for cell in cells})
    if not keys:
        return 0
    async with db.transaction() as conn:
        cursor = await conn.execute(
            "UPDATE nodes n SET status='retired' FROM runner_regions r, "
            "unnest(%s::text[],%s::text[],%s::text[]) w(provider,product,region) "
            "WHERE r.region_id=n.region_id AND r.slug=w.region AND n.provider=w.provider "
            "AND n.product=w.product AND n.status<>'retired'",
            ([k[0] for k in keys], [k[1] for k in keys], [k[2] for k in keys]),
        )
        return cursor.rowcount
