"""Opening a pulse slot: a region's countries spread over the slot (interval / N apart), one at
a time (pulse.py re-exports these names)."""

from __future__ import annotations

from datetime import datetime, timedelta

import psycopg

from .db import setting

GRACE = timedelta(seconds=60)  # a slot opened this late still uses its whole interval
MIN_GAP = timedelta(seconds=60)  # pulse starts of one region never closer: 10 s timeout + margin


def spread(
    countries: list[tuple[str, int]],
    base: datetime,
    window: timedelta,
    offset: float = 0.0,
    min_step: timedelta = timedelta(0),
) -> dict[tuple[str, int], datetime]:
    """Start times for (country, region_id) pairs: each region's countries, in code order,
    window / N apart from base (at least min_step), shifted by offset (0..1) of a step.
    Countries whose start would fall outside the window get none."""
    by_region: dict[int, list[str]] = {}
    for country, region in sorted(countries):
        by_region.setdefault(region, []).append(country)
    starts = {}
    for region, codes in by_region.items():
        step = max(window / len(codes), min_step)
        for i, country in enumerate(codes):
            if step * (i + offset) < window:
                starts[(country, region)] = base + step * (i + offset)
    return starts


async def open_slot(
    conn: psycopg.AsyncConnection,
    target_id: str,
    slot: datetime,
    every: timedelta,
    begin: datetime,
    offset: float = 0.0,
) -> list[int]:
    """Insert a pulse batch per enabled country, each region's countries every / N apart from
    pulse.lead_seconds after the slot start; offset (0..1 of a step) keeps the targets of one
    slot apart; no start is before begin + lead. A slot opened later than GRACE (a restart)
    spreads over its rest from begin, never closer than MIN_GAP, so pulses never overlap;
    countries that no longer fit wait for the next slot.

    Returns the new batch ids.
    """
    lead = timedelta(seconds=float(await setting(conn, "pulse.lead_seconds")))
    rows = await (
        await conn.execute(
            "SELECT c.country_code,c.region_id FROM countries c JOIN runner_regions r "
            "ON r.region_id=c.region_id AND r.enabled WHERE c.enabled"
        )
    ).fetchall()
    origin = slot if begin - slot <= GRACE else min(begin, slot + every)
    countries = [(r["country_code"].strip(), r["region_id"]) for r in rows]
    starts = spread(countries, origin + lead, slot + every - origin, offset, MIN_GAP)
    starts = {key: max(start, begin + lead) for key, start in starts.items()}
    return await insert_pulses(conn, target_id, slot, starts)


async def insert_pulses(
    conn: psycopg.AsyncConnection,
    target_id: str,
    slot: datetime,
    starts: dict[tuple[str, int], datetime],
) -> list[int]:
    """Insert one pulse batch per (country, region_id) at its start; returns the new ids."""
    keys = sorted(starts)
    inserted = await (
        await conn.execute(
            "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,"
            "start_at) SELECT 'pulse',u.c,u.r,%s,%s,u.s FROM unnest(%s::text[],%s::int[],"
            "%s::timestamptz[]) u(c,r,s) ORDER BY u.c ON CONFLICT DO NOTHING RETURNING batch_id",
            (
                target_id,
                slot,
                [k[0] for k in keys],
                [k[1] for k in keys],
                [starts[k] for k in keys],
            ),
        )
    ).fetchall()
    return [row["batch_id"] for row in inserted]
