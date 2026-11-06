"""Census, pulse and load batches: planning, the per-provider runs and the barrier.

A test_batches row is one synchronized slot (kind x country x target x scheduled_for) that every
participating product runs from its node in the country's region, starting at the same
`start_at`. Each product's part is a batch_runs row. A 'load' batch is the latency-under-load
test: every pulse slot of a census target also opens one per runner region, in the region's
reference country of the day (`load_references`, see `load_references()`), where each product
runs one wave of census_concurrency requests at census concurrency, load.delay_seconds after
that country's pulse. A product participates when it has at least one registered node outside
the browser region (whose 'browser' batches orchestrator/browser.py plans); a participating
product without a node in the batch's region gets a 'failed' run with stop_reason/error
'no_node' (plus an alert, and a 'no_node' tape event when that alert opens).

Lock order, everywhere: test_batches row (FOR UPDATE) first, then its batch_runs rows. The
barrier (`close_batch`) runs under the batch lock: a batch is 'done' once every run is terminal
and at least one finished, 'failed' when none did. What nodes call (assignments, progress,
uploads, failures) and the reaper are in nodework.py.

The settings `scheduler.census_enabled` / `scheduler.pulse_enabled` switch the planners off: a
planner that is off plans nothing and its job records 'skipped'. `census.every_minutes` replaces
the daily census slot with one every N minutes, aligned to the epoch, for every enabled country.

A census the provider spoiled (it never got a success, or lost its network mid-run) is run again
for that product alone, once per UTC day per country and target, as soon as one of its pulses
in the country gets through (`census_retry`); the rerun's retry_of names the census. A 'failed'
run is always our side's (no node, never started, went silent, node shut down or erred, upload
rejected) and is never rerun this way.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from ..browser.contracts import BROWSER_REGION_ID
from ..calculated.aggregate import blend, census_overlaps
from .db import Database, mark_closed, setting
from .events import emit, raise_alert
from .pulse import open_slot

log = logging.getLogger(__name__)

TERMINAL_RUNS = ("done", "failed", "skipped", "killed")
OPEN_BATCHES = ("scheduled", "running")
# Census slots are matched in [now - census.lead_seconds - CENSUS_GRACE, now + CENSUS_HORIZON):
# from a minute before the slot until two minutes past its start_at. A slot whose previous
# census is still open is deferred until start_at, so the window keeps at least two ticks at or
# after start_at: a late or missed tick (restart) still decides it. The UNIQUE(kind, country,
# target, scheduled_for) key keeps all of that idempotent.
CENSUS_HORIZON = timedelta(seconds=60)
CENSUS_GRACE = timedelta(seconds=120)
# A census planned after its start_at (a deferred slot, a restart) starts this long after now.
LATE_START = timedelta(seconds=5)
PLAN_LOCK = 817452902
TAKING = (
    "SELECT DISTINCT p.product_id,p.provider,p.name FROM nodes n JOIN products p ON "
    f"p.provider=n.provider AND p.name=n.product AND n.region_id<>{BROWSER_REGION_ID}"
)


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(UTC)


async def _rows(conn: psycopg.AsyncConnection, query: str, params: Any = None) -> list[dict]:
    return await (await conn.execute(query, params)).fetchall()


async def _row(conn: psycopg.AsyncConnection, query: str, params: Any = None) -> dict | None:
    return await (await conn.execute(query, params)).fetchone()


# --- planning ----------------------------------------------------------------------------


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
                "no_node",
                now,
                product_id=gap["product_id"],
                region_id=gap["region_id"],
                signal=-1,
                severity="major",
                data=data,
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


async def census_plan(db: Database, now: datetime | None = None) -> dict[str, Any]:
    """Create the census batches for every enabled country whose slot is due.

    The slot is the country's daily census_time, or with census.every_minutes = N every
    N-minute boundary since the epoch for every enabled country. Either way a slot is planned
    in [now - CENSUS_GRACE, now + CENSUS_HORIZON), so the minute-aligned planner sees it twice.
    One batch per census target, start_at = slot + census.lead_seconds (now + LATE_START when
    planned after that). Overlap is decided at start time: while the country's previous census
    (same target, earlier slot) is still scheduled or running, the slot is deferred (nothing is
    written) until start_at; a previous census that finished by then lets it plan normally, one
    still open at or after start_at turns the new batch 'skipped' with a 'census_skipped' event.
    """
    now = _now(now)
    await db.ensure_partition("test_events", now)
    async with db.transaction() as conn:
        if not await setting(conn, "scheduler.census_enabled"):
            return {"created": 0, "skipped": "census scheduler is off"}
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (PLAN_LOCK,))
        lead = timedelta(seconds=float(await setting(conn, "census.lead_seconds")))
        every = census_every(await setting(conn, "census.every_minutes"))
        products = await participants(conn)
        if not products:
            return {"created": 0, "skipped": 0, "reason": "no registered nodes"}
        window = {"now": now, "lo": now - lead - CENSUS_GRACE, "hi": now + CENSUS_HORIZON}
        if every is None:
            # Slots on the previous, current and next UTC day cover a window across midnight.
            due = await _rows(
                conn,
                "SELECT c.country_code,c.region_id,r.slug AS region,t.target_id,s.slot "
                "FROM countries c JOIN runner_regions r ON r.region_id=c.region_id AND r.enabled "
                "CROSS JOIN targets t CROSS JOIN generate_series(-1,1) d "
                "CROSS JOIN LATERAL (SELECT (((%(now)s AT TIME ZONE 'UTC')::date + d)"
                " + c.census_time) AT TIME ZONE 'UTC' AS slot) s "
                "WHERE c.enabled AND t.census AND s.slot >= %(lo)s AND s.slot < %(hi)s "
                "ORDER BY s.slot,c.country_code,t.sort_order,t.target_id",
                window,
            )
        else:
            # Every enabled country on the same aligned slots: floor(epoch / every) * every.
            due = await _rows(
                conn,
                "SELECT c.country_code,c.region_id,r.slug AS region,t.target_id,s.slot "
                "FROM countries c JOIN runner_regions r ON r.region_id=c.region_id AND r.enabled "
                "CROSS JOIN targets t CROSS JOIN unnest(%(slots)s::timestamptz[]) s(slot) "
                "WHERE c.enabled AND t.census "
                "ORDER BY s.slot,c.country_code,t.sort_order,t.target_id",
                {"slots": interval_slots(every, window["lo"], window["hi"])},
            )
        created, skipped, deferred = [], 0, 0
        for slot in due:
            key = (slot["country_code"], slot["target_id"], slot["slot"])
            if await _row(
                conn,
                "SELECT 1 FROM test_batches WHERE kind='census' AND country_code=%s "
                "AND target_id=%s AND scheduled_for=%s",
                key,
            ):
                continue
            previous = await _row(
                conn,
                "SELECT batch_id,status,scheduled_for FROM test_batches WHERE kind='census' "
                "AND country_code=%s AND target_id=%s AND status IN ('scheduled','running') "
                "AND scheduled_for<%s ORDER BY scheduled_for DESC LIMIT 1",
                key,
            )
            start_at = slot["slot"] + lead
            if previous and now < start_at:
                # It may still finish in time: the next tick decides.
                deferred += 1
                continue
            status = "skipped" if previous else "scheduled"
            inserted = await _row(
                conn,
                "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,"
                "start_at,status,finished_at) VALUES ('census',%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING RETURNING batch_id",
                (
                    slot["country_code"],
                    slot["region_id"],
                    slot["target_id"],
                    slot["slot"],
                    start_at if previous else max(start_at, now + LATE_START),
                    status,
                    now if previous else None,
                ),
            )
            if inserted is None:
                continue
            if previous:
                skipped += 1
                mark_closed(conn, inserted["batch_id"])
                await emit(
                    conn,
                    "census_skipped",
                    now,
                    batch_id=inserted["batch_id"],
                    country_code=slot["country_code"],
                    target_id=slot["target_id"],
                    region_id=slot["region_id"],
                    severity="minor",
                    data={
                        "region": slot["region"],
                        "reason": "previous census still " + previous["status"],
                        "previous_batch_id": previous["batch_id"],
                        "scheduled_for": slot["slot"].astimezone(UTC).isoformat(),
                    },
                )
            else:
                created.append(inserted["batch_id"])
        gaps = await _create_runs(conn, created, now)
        await _report_no_node(conn, gaps, now)
        for batch_id in created:
            await close_batch(conn, batch_id, now)
    return {
        "created": len(created),
        "skipped": skipped,
        "deferred": deferred,
        "no_node": len(gaps),
    }


def _countries(value: Any, name: str) -> dict[str, str]:
    """A {region slug: country code} setting, upper-cased."""
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and len(v) == 2 and v.isalpha()
        for k, v in value.items()
    ):
        raise ValueError(f"{name} must map region slugs to two-letter country codes")
    return {k: v.upper() for k, v in value.items()}


async def _reference_latency(
    conn: psycopg.AsyncConnection, day_start: datetime, days: int
) -> dict[str, dict[str, Any]]:
    """Per country, the light-load p50 of the census target over [day_start - days, day_start).

    From every product's done pulses of a census target whose run was ingested, except those
    that ran during a census of the same product (census_overlaps(): from stored run times). Each
    product's merged histogram weighs the same (blend()). Returns {country: {"p50", "successes"}}.
    """
    lo = day_start - timedelta(days=days)
    times = (
        "coalesce(r.started_at,br.started_at) AS started_at,"
        "least(coalesce(r.finished_at,br.finished_at,%(hi)s),%(hi)s) AS finished_at "
        "FROM batch_runs br JOIN test_batches b USING(batch_id) "
        "LEFT JOIN test_runs r ON r.run_key=br.run_key "
    )
    bounds = {"lo": lo, "hi": day_start, "margin": lo - timedelta(days=1)}
    pulses = await _rows(
        conn,
        f"SELECT br.product_id,br.batch_id,br.run_key,trim(b.country_code) AS country,{times}"
        "JOIN targets t ON t.target_id=b.target_id AND t.census WHERE b.kind='pulse' "
        "AND br.status='done' AND br.run_key IS NOT NULL AND b.scheduled_for>=%(lo)s "
        "AND b.scheduled_for<%(hi)s",
        bounds,
    )
    if not pulses:
        return {}
    census = await _rows(
        conn,
        f"SELECT br.product_id,br.batch_id,{times}WHERE b.kind='census' "
        "AND br.started_at IS NOT NULL AND br.started_at<%(hi)s "
        "AND coalesce(br.finished_at,%(hi)s)>=%(margin)s",
        bounds,
    )
    flagged = {o["run_key"] for o in census_overlaps(census, pulses)}
    clean = {p["run_key"]: p for p in pulses if p["run_key"] not in flagged}
    histograms: dict[str, dict[int, Counter]] = {}
    for row in await _rows(
        conn,
        "SELECT b.run_key,h.ms,sum(h.n)::bigint AS n FROM run_buckets b CROSS JOIN LATERAL "
        "histogram(b.histogram_steps,b.histogram_counts::bigint[]) h WHERE b.run_key=ANY(%s) "
        "AND b.bucket_start>=%s GROUP BY 1,2",
        (sorted(clean), lo - timedelta(hours=1)),
    ):
        run = clean[row["run_key"]]
        products = histograms.setdefault(run["country"], {})
        products.setdefault(run["product_id"], Counter())[row["ms"]] += row["n"]
    return {
        country: {
            "p50": blend(products.values())["p50"],
            "successes": sum(sum(h.values()) for h in products.values()),
        }
        for country, products in histograms.items()
    }


async def load_references(conn: psycopg.AsyncConnection, now: datetime) -> list[dict]:
    """Today's (UTC) latency-under-load reference country of every runner region with enabled
    countries, chosen on first use and stored in load_references (so every load batch of the day
    and every later calculation sees the same choice).

    Per region: an override (load.reference_overrides) that is one of its enabled countries
    ('override'); else the enabled country with the lowest light-load p50 over the trailing
    load.reference_days among those with at least load.reference_min_successes successes, ties
    broken by country code ('fastest'); else its default (load.reference_defaults, 'default');
    else its first enabled country by code ('first_enabled'). A stored choice whose country was
    disabled since is skipped (no load wave there today).
    """
    day = now.astimezone(UTC).date()
    regions = await _rows(
        conn,
        "SELECT r.region_id,r.slug,array_agg(trim(c.country_code) ORDER BY c.country_code) "
        "AS countries FROM runner_regions r JOIN countries c ON c.region_id=r.region_id "
        "AND c.enabled WHERE r.enabled GROUP BY 1,2 ORDER BY 1",
    )
    chosen = {
        row["region_id"]: row
        for row in await _rows(conn, "SELECT * FROM load_references WHERE day=%s", (day,))
    }
    missing = [r for r in regions if r["region_id"] not in chosen]
    if missing:
        overrides = _countries(
            await setting(conn, "load.reference_overrides"), "load.reference_overrides"
        )
        defaults = _countries(
            await setting(conn, "load.reference_defaults"), "load.reference_defaults"
        )
        minimum = int(await setting(conn, "load.reference_min_successes"))
        day_start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        stats = await _reference_latency(
            conn, day_start, int(await setting(conn, "load.reference_days"))
        )
        for region in missing:
            countries = region["countries"]
            candidates = sorted(
                (stats[c]["p50"], c)
                for c in countries
                if c in stats and stats[c]["p50"] is not None and stats[c]["successes"] >= minimum
            )
            if overrides.get(region["slug"]) in countries:
                country, basis = overrides[region["slug"]], "override"
            elif candidates:
                country, basis = candidates[0][1], "fastest"
            elif defaults.get(region["slug"]) in countries:
                country, basis = defaults[region["slug"]], "default"
            else:
                country, basis = countries[0], "first_enabled"
            await conn.execute(
                "INSERT INTO load_references(day,region_id,country_code,basis,p50_ms,successes,"
                "candidates) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    day,
                    region["region_id"],
                    country,
                    basis,
                    stats.get(country, {}).get("p50"),
                    stats.get(country, {}).get("successes"),
                    len(candidates),
                ),
            )
        chosen = {
            row["region_id"]: row
            for row in await _rows(conn, "SELECT * FROM load_references WHERE day=%s", (day,))
        }
    return [
        chosen[r["region_id"]]
        for r in regions
        if r["region_id"] in chosen
        and chosen[r["region_id"]]["country_code"].strip() in r["countries"]
    ]


async def pulse_plan(db: Database, now: datetime | None = None) -> dict[str, Any]:
    """Open the current slot of every pulse-enabled target that has no batch for it yet.

    Slots are aligned to the target's interval since the epoch (floor(now / interval)); a due
    target gets one batch per enabled country, each region's countries spread over the slot
    (pulse.spread). Targets added to the table are picked up on the next tick. A census target's
    slot also gets one 'load' batch per runner region, in the region's reference country of the
    day (load_references()), with the same scheduled_for, starting load.delay_seconds after the
    slot opens: kept apart so the pulse stays a light-load measurement. load.enabled switches
    the wave off.
    """
    now = _now(now)
    await db.ensure_partition("test_events", now)
    async with db.transaction() as conn:
        if not await setting(conn, "scheduler.pulse_enabled"):
            return {"targets": [], "created": 0, "skipped": "pulse scheduler is off"}
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (PLAN_LOCK,))
        lead = timedelta(seconds=float(await setting(conn, "pulse.lead_seconds")))
        loading = bool(await setting(conn, "load.enabled"))
        delay = timedelta(seconds=float(await setting(conn, "load.delay_seconds")))
        if not await participants(conn):
            return {"targets": 0, "created": 0, "reason": "no registered nodes"}
        targets = await _rows(
            conn,
            "SELECT target_id,pulse_interval_minutes,census FROM targets WHERE pulse_enabled "
            "ORDER BY sort_order,target_id",
        )
        created: list[int] = []
        opened = []
        loads = 0
        references: list[dict] | None = None
        for index, target in enumerate(targets):
            every = target["pulse_interval_minutes"] * 60
            slot = datetime.fromtimestamp(now.timestamp() // every * every, UTC)
            if await _row(
                conn,
                "SELECT 1 FROM test_batches WHERE kind='pulse' AND target_id=%s "
                "AND scheduled_for=%s LIMIT 1",
                (target["target_id"], slot),
            ):
                continue
            span, start = timedelta(seconds=every), max(slot, now) + lead
            offset = index / len(targets)
            created.extend(await open_slot(conn, target["target_id"], slot, span, now, offset))
            opened.append(target["target_id"])
            if target["census"] and loading:
                if references is None:
                    references = await load_references(conn, now)
                for reference in references:
                    # The reference country's own pulse runs next to it in the same slot.
                    load = await _row(
                        conn,
                        "INSERT INTO test_batches(kind,country_code,region_id,target_id,"
                        "scheduled_for,start_at) VALUES ('load',%s,%s,%s,%s,%s) "
                        "ON CONFLICT DO NOTHING RETURNING batch_id",
                        (
                            reference["country_code"],
                            reference["region_id"],
                            target["target_id"],
                            slot,
                            start + delay,
                        ),
                    )
                    if load:
                        created.append(load["batch_id"])
                        loads += 1
        gaps = await _create_runs(conn, created, now)
        await _report_no_node(conn, gaps, now)
        # Batches whose every run failed (no node in the region) close at once.
        for row in await _rows(
            conn,
            "SELECT batch_id FROM test_batches b WHERE batch_id=ANY(%s) AND NOT EXISTS "
            "(SELECT 1 FROM batch_runs r WHERE r.batch_id=b.batch_id AND r.status='queued')",
            (created,),
        ):
            await close_batch(conn, row["batch_id"], now)
    return {
        "targets": opened,
        "created": len(created) - loads,
        "load": loads,
        "no_node": len(gaps),
    }


# --- barrier -----------------------------------------------------------------------------


async def close_batch(conn: psycopg.AsyncConnection, batch_id: int, now: datetime) -> str:
    """The barrier: close the batch once every run is terminal. Call inside a transaction.

    Tape events are per product (census_started / census_finished, see run_finished); the
    batch itself writes none (pulse batches are detect.py's).
    """
    batch = await _row(
        conn, "SELECT status FROM test_batches WHERE batch_id=%s FOR UPDATE", (batch_id,)
    )
    if batch is None or batch["status"] not in OPEN_BATCHES:
        return batch["status"] if batch else "missing"
    runs = await _rows(conn, "SELECT status FROM batch_runs WHERE batch_id=%s", (batch_id,))
    if any(run["status"] not in TERMINAL_RUNS for run in runs):
        return batch["status"]
    status = "done" if any(run["status"] == "done" for run in runs) else "failed"
    await conn.execute(
        "UPDATE test_batches SET status=%s,finished_at=%s WHERE batch_id=%s",
        (status, now, batch_id),
    )
    mark_closed(conn, batch_id)
    return status


async def _run_event(conn: psycopg.AsyncConnection, batch_id: int, product_id: int) -> dict:
    return await _row(
        conn,
        "SELECT br.*,b.kind,b.country_code,b.target_id,b.region_id,b.start_at,"
        "r.slug AS region FROM batch_runs br JOIN test_batches b USING(batch_id) "
        "JOIN runner_regions r ON r.region_id=b.region_id "
        "WHERE br.batch_id=%s AND br.product_id=%s",
        (batch_id, product_id),
    )


def _signal(run: dict) -> int:
    if run["status"] == "failed" or run["stop_reason"] in ("no_success", "stalled"):
        return -1
    return 1 if run["status"] == "done" and run["stop_reason"] == "unique_ratio" else 0


async def run_finished(
    conn: psycopg.AsyncConnection, batch_id: int, product_id: int, now: datetime
) -> None:
    """census_finished for one product whose census run just became terminal.

    Signal +1 for a census that ended on the unique ratio, -1 for a failed run, one that
    never succeeded (no_success) or one whose network went down mid-run (stalled), 0 otherwise
    (killed, skipped, request cap). The admin's kill action should call this for each run it
    kills.
    """
    run = await _run_event(conn, batch_id, product_id)
    if run is None or run["kind"] != "census":
        return
    requests, successes = run["requests"], run["successes"]
    signal = _signal(run)
    await emit(
        conn,
        "census_finished",
        now,
        batch_id=batch_id,
        product_id=product_id,
        country_code=run["country_code"],
        target_id=run["target_id"],
        region_id=run["region_id"],
        signal=signal,
        severity="minor" if signal < 0 else "info",
        data={
            "region": run["region"],
            "requests": requests,
            "successes": successes,
            "unique_ips": run["unique_ips"],
            "success_rate": successes / requests if requests else None,
            "duration_s": (
                round(max((run["finished_at"] - run["started_at"]).total_seconds(), 0.0), 3)
                if run["started_at"] and run["finished_at"]
                else None
            ),
            "stop_reason": run["stop_reason"],
            "status": run["status"],
            "bytes_up": run["bytes_up"],
            "bytes_down": run["bytes_down"],
            **({"error": run["error"]} if run["error"] else {}),
        },
    )


def _moment(value: Any) -> datetime | None:
    """An ISO timestamp from a run summary, or None when absent or malformed."""
    try:
        moment = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return moment if moment is not None and moment.tzinfo is not None else None


async def census_recovered(
    conn: psycopg.AsyncConnection,
    batch_id: int,
    product_id: int,
    summary: dict[str, Any],
    now: datetime,
) -> int:
    """A census that had successes closes the product's open census outages in its country.

    They end when this run started (the network answered then); outages that opened after it
    started are left alone. Each closed outage writes one 'recovered' event.
    """
    run = await _run_event(conn, batch_id, product_id)
    if run is None or run["kind"] != "census" or run["status"] != "done" or not run["successes"]:
        return 0
    began = _moment(summary.get("started_at")) or run["started_at"]
    closed = await _rows(
        conn,
        "UPDATE outages SET ended_at=greatest(started_at,%s) WHERE product_id=%s "
        "AND country_code=%s AND source='census' AND ended_at IS NULL AND started_at<%s "
        "RETURNING outage_id,batch_id,started_at,ended_at",
        (began, product_id, run["country_code"], began),
    )
    for outage in closed:
        await emit(
            conn,
            "recovered",
            now,
            batch_id=batch_id,
            product_id=product_id,
            country_code=run["country_code"],
            target_id=run["target_id"],
            region_id=run["region_id"],
            signal=1,
            data={
                "region": run["region"],
                "source": "census",
                "incident": "down",
                "since": outage["started_at"].astimezone(UTC).isoformat(),
                "duration_s": round((outage["ended_at"] - outage["started_at"]).total_seconds(), 3),
                "outage_batch_id": outage["batch_id"],
            },
        )
    return len(closed)


async def census_down(
    conn: psycopg.AsyncConnection,
    batch_id: int,
    product_id: int,
    summary: dict[str, Any],
    now: datetime,
) -> bool:
    """A census that stalled (successes, then none for census.stall_seconds) opens an outage.

    It starts at the run's last success and stays open (ended_at NULL) until a healthy pulse
    or a census with successes in the country closes it. A 'down' event (source 'census')
    goes on the tape. Nothing is opened while the product already has an open outage there.
    """
    run = await _run_event(conn, batch_id, product_id)
    since = _moment(summary.get("outage_started_at"))
    if run is None or run["kind"] != "census" or run["stop_reason"] != "stalled" or since is None:
        return False
    opened = await _row(
        conn,
        "INSERT INTO outages(product_id,country_code,region_id,source,batch_id,started_at) "
        "SELECT %(product)s,%(country)s,%(region)s,'census',%(batch)s,%(since)s "
        "WHERE NOT EXISTS (SELECT 1 FROM outages WHERE product_id=%(product)s "
        "AND country_code=%(country)s AND (ended_at IS NULL OR batch_id=%(batch)s)) "
        "RETURNING outage_id",
        {
            "product": product_id,
            "country": run["country_code"],
            "region": run["region_id"],
            "batch": batch_id,
            "since": since,
        },
    )
    if opened is None:
        return False
    await emit(
        conn,
        "down",
        now,
        batch_id=batch_id,
        product_id=product_id,
        country_code=run["country_code"],
        target_id=run["target_id"],
        region_id=run["region_id"],
        signal=-1,
        severity="critical",
        data={
            "region": run["region"],
            "since": since.astimezone(UTC).isoformat(),
            "source": "census",
            "batch_id": batch_id,
            "requests": run["requests"],
            "successes": run["successes"],
        },
    )
    return True


def _spoiled(run: dict) -> bool:
    """Whether a census run ended because the provider was down: a done run that never got a
    success (no_success) or lost its network mid-run (stalled). A failed run is our side's."""
    return run["status"] == "done" and run["stop_reason"] in ("no_success", "stalled")


async def census_retry(
    conn: psycopg.AsyncConnection, batch_id: int, product_id: int, now: datetime
) -> list[int]:
    """Rerun the census a provider outage spoiled, at the first pulse that sees it up again.

    Called when a run finished. For a pulse with successes, every census target whose latest
    census run of this product in the country was spoiled (_spoiled) gets a census batch for
    this product alone, starting now (retry_of = that census), unless one was already rerun
    there today (UTC) or a census for the country and target is still open. A census_retry
    event goes on the tape. Returns the new batch ids.
    """
    run = await _run_event(conn, batch_id, product_id)
    if run is None or run["kind"] != "pulse" or run["status"] != "done" or not run["successes"]:
        return []
    country = run["country_code"]
    day = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    latest = await _rows(
        conn,
        "SELECT DISTINCT ON (b.target_id) b.batch_id,b.target_id,b.region_id,br.status,"
        "br.stop_reason,br.successes,br.error FROM batch_runs br JOIN test_batches b "
        "USING(batch_id) JOIN targets t ON t.target_id=b.target_id "
        "WHERE b.kind='census' AND t.census AND br.product_id=%s AND b.country_code=%s "
        "AND br.status IN ('done','failed') "
        "ORDER BY b.target_id,b.scheduled_for DESC,b.batch_id DESC",
        (product_id, country),
    )
    created = []
    for census in latest:
        if not _spoiled(census):
            continue
        key = {"country": country, "target": census["target_id"], "product": product_id}
        if await _row(
            conn,
            "SELECT 1 FROM test_batches b JOIN batch_runs br USING(batch_id) "
            "WHERE b.kind='census' AND b.retry_of IS NOT NULL AND b.country_code=%(country)s "
            "AND b.target_id=%(target)s AND br.product_id=%(product)s AND b.scheduled_for>=%(day)s",
            key | {"day": day},
        ) or await _row(
            conn,
            "SELECT 1 FROM test_batches WHERE kind='census' AND country_code=%(country)s "
            "AND target_id=%(target)s AND status IN ('scheduled','running')",
            key,
        ):
            continue
        retry = await _row(
            conn,
            "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,"
            "start_at,retry_of) VALUES ('census',%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT DO NOTHING RETURNING batch_id",
            (
                country,
                run["region_id"],
                census["target_id"],
                now,
                now + LATE_START,
                census["batch_id"],
            ),
        )
        if retry is None:
            continue
        # The pulse's node is the product's node in the region.
        await conn.execute(
            "INSERT INTO batch_runs(batch_id,product_id,node_id,status) VALUES (%s,%s,%s,'queued')",
            (retry["batch_id"], product_id, run["node_id"]),
        )
        await emit(
            conn,
            "census_retry",
            now,
            batch_id=retry["batch_id"],
            product_id=product_id,
            country_code=country,
            target_id=census["target_id"],
            region_id=run["region_id"],
            data={
                "region": run["region"],
                "retry_of": census["batch_id"],
                "reason": census["stop_reason"] or census["error"],
                "pulse_batch_id": batch_id,
            },
        )
        created.append(retry["batch_id"])
    return created


async def _start(conn: psycopg.AsyncConnection, batch_id: int, product_id: int, now: datetime):
    """First sign of life from a run: the run starts (census_started for its product), and
    the batch with its first run."""
    batch = await _row(
        conn, "SELECT status FROM test_batches WHERE batch_id=%s FOR UPDATE", (batch_id,)
    )
    await conn.execute(
        "UPDATE batch_runs SET started_at=coalesce(started_at,%s) "
        "WHERE batch_id=%s AND product_id=%s",
        (now, batch_id, product_id),
    )
    if batch["status"] == "scheduled":
        await conn.execute(
            "UPDATE test_batches SET status='running',started_at=%s WHERE batch_id=%s",
            (now, batch_id),
        )
    run = await _run_event(conn, batch_id, product_id)
    if run["kind"] == "census":
        await emit(
            conn,
            "census_started",
            now,
            batch_id=batch_id,
            product_id=product_id,
            country_code=run["country_code"],
            target_id=run["target_id"],
            region_id=run["region_id"],
            data={"region": run["region"], "start_at": run["start_at"].astimezone(UTC).isoformat()},
        )
