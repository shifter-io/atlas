"""Pulse planning and the network-wide pulse evaluator.

Planning: each runner region's enabled countries are spread evenly over a slot (interval / N
apart), so a region's node runs one country at a time and never a burst of concurrent pulses.

Evaluation (job pulse_evaluate): pulse runs keep only their 5-minute run_buckets, no per-IP
rows. Each complete window sums a network's connection errors (timeouts + proxy errors) over
every region. A window is down when the summed error share is at least pulse.down_error_share
and at least pulse.down_min_regions regions are each over that share; the network's outage
then opens as one outages row per enabled country (source 'pulse', batch_id NULL, which is how
a network outage is told apart from a per-batch one) and a pulse_down event lists them. After
pulse.recover_windows healthy windows the rows close and pulse_recovered lists them. A window
without pulse data is neutral. Latency: latency_rise fires when a network's last
pulse.latency_windows done pulse runs in a country all have a p50 at least
pulse.latency_ratio x the baseline, the p50 of its latest done census run there (the daily
test); a lower pulse never moves the baseline.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from ..detect import LOCK as DETECT_LOCK
from ..detect import RUN_P50_SQL
from .db import Database, put_setting, setting
from .events import (
    LatencyRise,
    PulseWindow,
    emit_latency_rise,
    emit_pulse_down,
    emit_pulse_recovered,
)

WINDOW = timedelta(minutes=5)
MAX_WINDOWS = 12  # windows evaluated per tick when catching up
GRACE = timedelta(seconds=60)  # a slot opened this late still uses its whole interval


def spread(
    countries: list[tuple[str, int]], base: datetime, window: timedelta, offset: float = 0.0
) -> dict[tuple[str, int], datetime]:
    """Start times for (country, region_id) pairs: each region's countries, in code order,
    window / N apart from base, shifted by offset (0..1) of a step."""
    by_region: dict[int, list[str]] = {}
    for country, region in sorted(countries):
        by_region.setdefault(region, []).append(country)
    starts = {}
    for region, codes in by_region.items():
        step = window / len(codes)
        for i, country in enumerate(codes):
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
    """Insert a pulse batch per enabled country, spread over the slot (every / N apart, from
    pulse.lead_seconds after its start) or, when opened later than GRACE (a restart), over its
    rest from begin; no start is before begin + lead. offset (0..1 of a step) keeps the targets
    of one slot apart.

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
    starts = spread(countries, origin + lead, slot + every - origin, offset)
    starts = {key: max(start, begin + lead) for key, start in starts.items()}
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


# --- evaluation --------------------------------------------------------------------------


async def _settings(conn: psycopg.AsyncConnection) -> dict[str, Any]:
    keys = (
        "down_error_share",
        "down_min_regions",
        "recover_windows",
        "latency_ratio",
        "latency_windows",
        "settle_seconds",
        "evaluated_until",
    )
    return {key: await setting(conn, f"pulse.{key}") for key in keys}


async def window_stats(
    conn: psycopg.AsyncConnection, start: datetime, share: float
) -> dict[int, dict[str, int]]:
    """Per network: requests and connection errors over all regions in the window, the
    regions with data and the regions whose own error share reached `share`."""
    rows = await (
        await conn.execute(
            "SELECT br.product_id,b.region_id,sum(rb.completed)::bigint AS requests,"
            "sum(rb.timeouts+rb.proxy_errors)::bigint AS errors FROM run_buckets rb "
            "JOIN batch_runs br ON br.run_key=rb.run_key "
            "JOIN test_batches b ON b.batch_id=br.batch_id "
            "WHERE b.kind='pulse' AND rb.bucket_start=%s GROUP BY 1,2",
            (start,),
        )
    ).fetchall()
    out: dict[int, dict[str, int]] = {}
    for row in rows:
        stats = out.setdefault(
            row["product_id"], {"requests": 0, "errors": 0, "regions": 0, "over": 0}
        )
        requests, errors = int(row["requests"] or 0), int(row["errors"] or 0)
        stats["requests"] += requests
        stats["errors"] += errors
        if requests:
            stats["regions"] += 1
            stats["over"] += int(errors >= share * requests)
    return out


def is_down(stats: dict[str, int] | None, share: float, min_regions: int) -> bool | None:
    """True down, False healthy, None when the window has no pulse data for the network."""
    if not stats or not stats["requests"]:
        return None
    return stats["errors"] >= share * stats["requests"] and stats["over"] >= min_regions


async def _open_network(conn: psycopg.AsyncConnection, product_id: int) -> list[dict]:
    return await (
        await conn.execute(
            "SELECT outage_id,started_at FROM outages WHERE product_id=%s AND source='pulse' "
            "AND batch_id IS NULL AND ended_at IS NULL",
            (product_id,),
        )
    ).fetchall()


async def _open_outage(
    conn: psycopg.AsyncConnection, now: datetime, start: datetime, product: int, window: PulseWindow
) -> None:
    rows = await (
        await conn.execute(
            "INSERT INTO outages(product_id,country_code,region_id,source,started_at) "
            "SELECT %s,c.country_code,c.region_id,'pulse',%s FROM countries c "
            "JOIN runner_regions r ON r.region_id=c.region_id AND r.enabled WHERE c.enabled "
            "AND NOT EXISTS (SELECT 1 FROM outages o WHERE o.product_id=%s "
            "AND o.country_code=c.country_code AND o.ended_at IS NULL) "
            "ORDER BY c.country_code RETURNING outage_id",
            (product, start, product),
        )
    ).fetchall()
    if rows:
        ids = tuple(row["outage_id"] for row in rows)
        await emit_pulse_down(conn, now, window, outage_ids=ids)


async def _recovered(
    conn: psycopg.AsyncConnection, start: datetime, product: int, cfg: dict[str, Any]
) -> bool:
    """True when this window and the recover_windows - 1 before it are all healthy."""
    for k in range(int(cfg["recover_windows"])):
        stats = (await window_stats(conn, start - WINDOW * k, cfg["down_error_share"])).get(product)
        if is_down(stats, cfg["down_error_share"], int(cfg["down_min_regions"])) is not False:
            return False
    return True


async def _close_outage(
    conn: psycopg.AsyncConnection, now: datetime, product: int, at: datetime, open_rows: list
) -> None:
    ids = tuple(row["outage_id"] for row in open_rows)
    await conn.execute(
        "UPDATE outages SET ended_at=greatest(started_at,%s) WHERE outage_id=ANY(%s)",
        (at, list(ids)),
    )
    down_since = min(row["started_at"] for row in open_rows)
    await emit_pulse_recovered(
        conn, max(now, down_since), product_id=product, down_since=down_since, outage_ids=ids
    )


async def evaluate_window(
    conn: psycopg.AsyncConnection, start: datetime, now: datetime, cfg: dict[str, Any]
) -> dict[str, int]:
    """Open and close network outages for one complete window. Call inside a transaction."""
    share, min_regions = float(cfg["down_error_share"]), int(cfg["down_min_regions"])
    opened = closed = 0
    for product, stats in sorted((await window_stats(conn, start, share)).items()):
        state = is_down(stats, share, min_regions)
        open_rows = await _open_network(conn, product)
        if state and not open_rows:
            window = PulseWindow(
                product_id=product,
                window_start=start,
                window_minutes=int(WINDOW.total_seconds() // 60),
                errors=stats["errors"],
                requests=stats["requests"],
                regions=stats["over"],
                threshold=share,
            )
            await _open_outage(conn, now, start, product, window)
            opened += 1
        elif state is False and open_rows and await _recovered(conn, start, product, cfg):
            first = start - WINDOW * (int(cfg["recover_windows"]) - 1)
            await _close_outage(conn, now, product, first, open_rows)
            closed += 1
    return {"opened": opened, "closed": closed}


async def _p50s(conn: psycopg.AsyncConnection, run_keys: list[str]) -> dict[str, int]:
    if not run_keys:
        return {}
    rows = await (await conn.execute(RUN_P50_SQL, (sorted(set(run_keys)),))).fetchall()
    return {row["run_key"]: row["p50"] for row in rows}


async def _baseline(conn: psycopg.AsyncConnection, product: int, country: str) -> int | None:
    row = await (
        await conn.execute(
            "SELECT br.run_key FROM batch_runs br JOIN test_batches b ON b.batch_id=br.batch_id "
            "WHERE b.kind='census' AND br.product_id=%s AND b.country_code=%s "
            "AND br.status='done' AND br.run_key IS NOT NULL ORDER BY b.start_at DESC LIMIT 1",
            (product, country),
        )
    ).fetchone()
    return (await _p50s(conn, [row["run_key"]])).get(row["run_key"]) if row else None


async def _recent_pulses(
    conn: psycopg.AsyncConnection, product: int, country: str, before: datetime, limit: int
) -> list[dict]:
    """The newest done pulse runs that started before `before`, newest first."""
    return await (
        await conn.execute(
            "SELECT br.run_key,b.region_id FROM batch_runs br JOIN test_batches b "
            "ON b.batch_id=br.batch_id WHERE b.kind='pulse' AND br.product_id=%s "
            "AND b.country_code=%s AND br.status='done' AND br.run_key IS NOT NULL "
            "AND b.start_at<%s ORDER BY b.start_at DESC,b.batch_id DESC LIMIT %s",
            (product, country, before, limit),
        )
    ).fetchall()


def _above(p50: int | None, baseline: int, ratio: float) -> bool:
    return p50 is not None and p50 > baseline and p50 >= ratio * baseline


async def evaluate_latency(
    conn: psycopg.AsyncConnection, start: datetime, now: datetime, cfg: dict[str, Any]
) -> int:
    """latency_rise for each network and country whose newest done pulse run began in this
    window and completes a streak of latency_windows runs above ratio x baseline."""
    ratio, streak = float(cfg["latency_ratio"]), int(cfg["latency_windows"])
    pairs = await (
        await conn.execute(
            "SELECT DISTINCT br.product_id,b.country_code FROM batch_runs br "
            "JOIN test_batches b ON b.batch_id=br.batch_id WHERE b.kind='pulse' "
            "AND br.status='done' AND (SELECT min(rb.bucket_start) FROM run_buckets rb "
            "WHERE rb.run_key=br.run_key)=%s ORDER BY 1,2",
            (start,),
        )
    ).fetchall()
    fired = 0
    for pair in pairs:
        product, country = pair["product_id"], pair["country_code"].strip()
        baseline = await _baseline(conn, product, country)
        if baseline is None:
            continue
        runs = await _recent_pulses(conn, product, country, start + WINDOW, streak + 1)
        p50 = await _p50s(conn, [r["run_key"] for r in runs])
        above = [_above(p50.get(r["run_key"]), baseline, ratio) for r in runs]
        # Fire once per streak: the run before the streak must not be above too.
        if len(above) < streak or not all(above[:streak]) or (len(above) > streak and above[-1]):
            continue
        rise = LatencyRise(
            product_id=product,
            country_code=country,
            p50=float(p50[runs[0]["run_key"]]),
            baseline=float(baseline),
            region_id=runs[0]["region_id"],
        )
        await emit_latency_rise(conn, now, rise)
        fired += 1
    return fired


def _floor(at: datetime) -> datetime:
    seconds = WINDOW.total_seconds()
    return datetime.fromtimestamp(at.timestamp() // seconds * seconds, UTC)


async def evaluate(db: Database, now: datetime | None = None) -> dict[str, Any]:
    """Evaluate every complete, settled window since the last one (at most MAX_WINDOWS)."""
    now = now or datetime.now(UTC)
    await db.ensure_partition("test_events", now)
    totals = {"windows": 0, "opened": 0, "closed": 0, "latency_rise": 0}
    async with db.transaction() as conn:
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (DETECT_LOCK,))
        cfg = await _settings(conn)
        last = _floor(now - timedelta(seconds=float(cfg["settle_seconds"]))) - WINDOW
        done = cfg["evaluated_until"]
        start = datetime.fromisoformat(done) + WINDOW if done else last
        start = max(start, last - WINDOW * (MAX_WINDOWS - 1))
        while start <= last:
            for key, value in (await evaluate_window(conn, start, now, cfg)).items():
                totals[key] += value
            totals["latency_rise"] += await evaluate_latency(conn, start, now, cfg)
            totals["windows"] += 1
            await put_setting(conn, "pulse.evaluated_until", start.isoformat())
            start += WINDOW
    return totals
