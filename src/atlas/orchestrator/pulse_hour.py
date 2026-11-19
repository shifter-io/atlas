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
outage (orchestrator/pulse.py).

A cell's runs go to the node of the cell's own region, and each batch is in its country's region
(countries.region_id): a cell may hold countries of another region (PULSE-REPLACE-1: a lost
cell's countries handed to the same network's droplet elsewhere), whose runs then go from that
droplet, in those countries' batches. A second call for the same hour adds only what is new:
the batches of countries not yet opened (UNIQUE(kind, country, target, scheduled_for)) and the
runs of newly armed cells in the hour's batches still to start (PRIMARY KEY(batch_id,
product_id)).

PROXY-CHEAP-BUDGET-1: a network with a connection budget pulses after its daily runs of the hour
(campaign.pulse_late). Its cells are opened later in their own slot, `slot` (a whole minute of
the hour, the batches' scheduled_for, so they are never the other networks' HH:00 batches),
with the starts up to `last_start` past the hour. PULSE-STEP-2: a network with a pulse step
(daily.budget.PULSE_STEP_SECONDS) is not opened on a clock: `queues` lists each armed node's
runs in order, and campaign.pulse_late gives a node its next one (`open_run`, starting now)
when it has a free slot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from .batches import PLAN_LOCK, _close_unmanned
from .db import Database, setting
from .pulse_slots import insert_pulses
from .sqlrows import _now, _row, _rows

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


def window(hour: datetime, begin: datetime, last: timedelta = LAST_START) -> timedelta:
    """How long after `begin` a region's starts may come: until `last` past the hour."""
    return max(timedelta(0), hour + last - begin)


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
    """The enabled (country, region_id) pairs of the cells' countries, in enabled regions."""
    wanted = sorted({c for cell in cells for c in cell.countries})
    rows = await _rows(
        conn,
        "SELECT c.country_code,c.region_id FROM countries c JOIN runner_regions r "
        "ON r.region_id=c.region_id AND r.enabled WHERE c.enabled "
        "AND c.country_code=ANY(%s::text[]) ORDER BY 1",
        (wanted,),
    )
    return [(row["country_code"].strip(), row["region_id"]) for row in rows]


async def _targets(conn: psycopg.AsyncConnection) -> list[dict]:
    """The pulse-enabled targets, in their order, with their pulse interval."""
    return await _rows(
        conn,
        "SELECT target_id,pulse_interval_minutes FROM targets WHERE pulse_enabled "
        "ORDER BY sort_order,target_id",
    )


async def _hour_batches(
    conn: psycopg.AsyncConnection, slot: datetime, targets: list, now: datetime
) -> list:
    """The hour's pulse batches of its hourly targets still to start, an earlier call's too: a
    closed batch or one already due takes no new run (it would never be claimed, or fail)."""
    rows = await _rows(
        conn,
        "SELECT batch_id FROM test_batches WHERE kind='pulse' AND scheduled_for=%s "
        "AND target_id=ANY(%s) AND status='scheduled' AND start_at>%s ORDER BY batch_id",
        (slot, targets, now),
    )
    return [row["batch_id"] for row in rows]


async def _runs(conn: psycopg.AsyncConnection, ids: list[int], cells: list[Armed]) -> int:
    """One queued run per batch and armed product whose cell holds the batch's country, for the
    node of the cell's region."""
    keys = {(cell.provider, cell.product, c, cell.region) for cell in cells for c in cell.countries}
    rows = sorted(keys)
    cursor = await conn.execute(
        "INSERT INTO batch_runs(batch_id,product_id,node_id,status) "
        "SELECT b.batch_id,p.product_id,n.node_id,'queued' FROM test_batches b "
        "JOIN unnest(%s::text[],%s::text[],%s::text[],%s::text[]) w(provider,product,country,"
        "region) ON w.country=b.country_code JOIN products p ON p.provider=w.provider "
        "AND p.name=w.product JOIN runner_regions nr ON nr.slug=w.region "
        "JOIN nodes n ON n.region_id=nr.region_id AND n.provider=w.provider "
        "AND n.product=w.product WHERE b.batch_id=ANY(%s) ON CONFLICT DO NOTHING",
        tuple([row[i] for row in rows] for i in range(4)) + (ids,),
    )
    return cursor.rowcount


async def open_hour(
    db: Database,
    hour: datetime,
    cells: list[Armed],
    now: datetime | None = None,
    *,
    slot: datetime | None = None,
    last_start: timedelta = LAST_START,
) -> dict[str, Any]:
    """Open the hour's pulse batches and runs for the armed cells in `slot` (the hour by
    default), a country every pulse.step_seconds, no start later than `last_start` past the
    hour; returns what it did."""
    now, hour = _now(now), hour.astimezone(UTC)
    if hour.minute or hour.second or hour.microsecond:
        raise ValueError("the pulse hour must be a whole UTC hour")
    slot = hour if slot is None else slot.astimezone(UTC)
    if slot.second or slot.microsecond or not hour <= slot < hour + timedelta(hours=1):
        raise ValueError("a pulse slot must be a whole minute of its hour")
    async with db.transaction() as conn:
        if not await setting(conn, "pulse.on_demand"):
            return {"targets": [], "created": 0, "skipped": "pulse.on_demand is off"}
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (PLAN_LOCK,))
        lead = timedelta(seconds=float(await setting(conn, "pulse.lead_seconds")))
        step = timedelta(seconds=float(await setting(conn, "pulse.step_seconds")))
        begin = max(now, slot) + lead
        span = window(hour, begin, last_start)
        if span <= timedelta(0):
            return {"targets": [], "created": 0, "skipped": "no time left in the hour"}
        targets = await _targets(conn)
        hourly = [t for t in targets if t["pulse_interval_minutes"] == 60]
        countries = await _countries(conn, cells)
        created, opened = [], []
        for index, target in enumerate(hourly):
            starts = chain(countries, begin, step, index / len(hourly), span)
            ids = await insert_pulses(conn, target["target_id"], slot, starts)
            if ids:
                created.extend(ids)
                opened.append(target["target_id"])
        hour_ids = await _hour_batches(conn, slot, [t["target_id"] for t in hourly], now)
        runs = await _runs(conn, hour_ids, cells) if hour_ids else 0
        if created:
            await _close_unmanned(conn, created, now)
    return {
        "targets": opened,
        "created": len(created),
        "runs": runs,
        "not_hourly": [t["target_id"] for t in targets if t not in hourly],
    }


async def queues(db: Database, cells: list[Armed]) -> tuple[dict, timedelta, int]:
    """PULSE-STEP-2: each armed node's runs, (country, region_id, target) in country code order
    then the targets' (by its (provider, product, region)), pulse.step_seconds and the number of
    hourly targets; no run when pulse.on_demand is off."""
    async with db.connection() as conn:
        step = timedelta(seconds=float(await setting(conn, "pulse.step_seconds")))
        if not await setting(conn, "pulse.on_demand"):
            return {}, step, 0
        hourly = [t["target_id"] for t in await _targets(conn) if t["pulse_interval_minutes"] == 60]
        found: dict[tuple[str, str, str], list[tuple[str, int, str]]] = {}
        for cell in cells:
            runs = [(c, r, t) for c, r in await _countries(conn, [cell]) for t in hourly]
            found.setdefault((cell.provider, cell.product, cell.region), []).extend(runs)
    for runs in found.values():
        runs.sort(key=lambda run: run[:2])  # stable: a country's targets stay in their order
    return found, step, len(hourly)


async def open_run(
    db: Database, slot: datetime, node: tuple[str, str, str], run: tuple[str, int, str], start
) -> tuple[int, int]:
    """PULSE-STEP-2: one queued pulse run for `node` (provider, product, region) of `run`
    (country, region_id, target) in `slot`, starting at `start`: in a new batch, or in the slot's
    open one of another node with its own start (batch_runs.start_at). Returns (batches
    created, runs added)."""
    country, region, target = run
    async with db.transaction() as conn:
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (PLAN_LOCK,))
        created = await insert_pulses(conn, target, slot, {(country, region): start})
        batch = await _row(
            conn,
            "SELECT batch_id,start_at FROM test_batches WHERE kind='pulse' AND country_code=%s "
            "AND target_id=%s AND scheduled_for=%s AND status IN ('scheduled','running')",
            (country, target, slot),
        )
        if batch is None:
            return len(created), 0  # the slot's batch of that country is closed already
        own = None if batch["start_at"] == start else start
        cursor = await conn.execute(
            "INSERT INTO batch_runs(batch_id,product_id,node_id,status,start_at) "
            "SELECT %s,p.product_id,n.node_id,'queued',%s FROM products p JOIN nodes n "
            "ON n.provider=p.provider AND n.product=p.name JOIN runner_regions r "
            "ON r.region_id=n.region_id WHERE p.provider=%s AND p.name=%s AND r.slug=%s "
            "ON CONFLICT DO NOTHING",
            (batch["batch_id"], own, *node),
        )
        if created:
            await _close_unmanned(conn, created, start)
    return len(created), cursor.rowcount


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
