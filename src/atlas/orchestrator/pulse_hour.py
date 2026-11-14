"""PULSE-ONDEMAND-1: one hour's pulses for the on-demand droplets armed for it.

With the setting `pulse.on_demand` the minute planners plan nothing (batches.py); the hourly
pulse job creates one droplet per network and region (daily.pulses), arms each as an `atlas
node` and then calls `open_hour` with the cells it armed. For every pulse-enabled target it
inserts one pulse batch per country of those cells (scheduled_for = the hour, so the target's
pulse_interval_minutes must be 60; a target with another interval is skipped and reported) and
one batch_runs row per armed product whose cell holds the country. A product without a droplet
in the hour gets no run, so no `no_node` failure or alert.

A region's countries run back to back (PULSE-COMPACT-1): from now (never before the hour) +
pulse.lead_seconds, one country every `pulse.step_seconds` (12), in code order, the targets half
a step apart; a pulse of 100 requests at 25 takes about 10 to 15 s, so a droplet of 57 countries
is done in about 12 minutes and every country is sampled once in the first quarter of the hour
(HH:00 to about HH:15). No start is later than LAST_START past the hour (the droplet's hard stop
is 25 minutes after its creation at HH:57, campaign.pulse_ondemand.HARD_STOP); a country that
no longer fits waits for the next hour, and an hour with no time left opens nothing. The quiet
rest of the hour has no pulse data, which the pulse evaluator treats as neutral, never as an
outage (orchestrator/pulse.py). A second call for the same hour
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
from .pulse_slots import insert_pulses
from .sqlrows import _now, _rows

# The last pulse of the hour starts by HH:18, so it is stored before the droplet's hard stop
# (campaign.pulse_ondemand.HARD_STOP, 25 minutes after its creation at HH:57).
LAST_START = timedelta(minutes=18)


@dataclass(frozen=True, slots=True)
class Armed:
    """A droplet armed for the hour: its network's provider and product, region and countries."""

    provider: str
    product: str
    region: str
    countries: tuple[str, ...]


def window(hour: datetime, begin: datetime) -> timedelta:
    """How long after `begin` a region's starts may come: until LAST_START past the hour."""
    return max(timedelta(0), hour + LAST_START - begin)


def chain(
    countries: list[tuple[str, int]], begin: datetime, step: timedelta, offset: float, span
) -> dict[tuple[str, int], datetime]:
    """Back-to-back starts for (country, region_id) pairs: each region's countries in code order
    `step` apart from begin, shifted by offset (0..1) of a step; none at or after begin + span."""
    by_region: dict[int, list[str]] = {}
    for country, region in sorted(countries):
        by_region.setdefault(region, []).append(country)
    starts = {}
    for region, codes in by_region.items():
        for i, country in enumerate(codes):
            if step * (i + offset) < span:
                starts[(country, region)] = begin + step * (i + offset)
    return starts


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
        step = timedelta(seconds=float(await setting(conn, "pulse.step_seconds")))
        begin = max(now, hour) + lead
        span = window(hour, begin)
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
            starts = chain(countries, begin, step, index / len(hourly), span)
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
