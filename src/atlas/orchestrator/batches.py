"""Census, pulse and load batches: planning, the per-provider runs and the barrier.

A test_batches row is one synchronized slot (kind x country x target x scheduled_for) that every
participating product runs from its node in the country's region, starting at the same
`start_at`. Each product's part is a batch_runs row. A 'load' batch is the latency-under-load
test: every pulse slot of a census target also opens one per runner region, in the region's
reference country of the day (`load_references`, see references.py), where each product
runs one wave of census_concurrency requests at census concurrency, load.delay_seconds after
that country's pulse. A product participates when it has at least one registered node outside
the browser region (whose 'browser' batches orchestrator/browser.py plans); a participating
product without a node in the batch's region gets a 'failed' run with stop_reason/error
'no_node' (plus an alert, and a 'no_node' tape event when that alert opens). Those shared
planning parts, and the aligned slots, are in planparts.py.

Lock order, everywhere: test_batches row (FOR UPDATE) first, then its batch_runs rows. The
barrier (`close_batch`) runs under the batch lock: a batch is 'done' once every run is terminal
and at least one finished, 'failed' when none did. What nodes call (assignments, progress,
uploads, failures) and the reaper are in nodework.py.

The settings `scheduler.census_enabled` / `scheduler.pulse_enabled` switch the planners off: a
planner that is off plans nothing and its job records 'skipped'. So does `pulse.on_demand`: its
nodes are droplets that exist only during their hour, which the hourly pulse job opens itself
(pulse_hour.py). `census.every_minutes` replaces
the daily census slot with one every N minutes, aligned to the epoch, for every enabled country.

A census the provider spoiled (it never got a success, or lost its network mid-run) is run again
for that product alone, once per UTC day per country and target, as soon as one of its pulses
in the country gets through (`census_retry`); the rerun's retry_of names the census. A 'failed'
run is always our side's (no node, never started, went silent, node shut down or erred, upload
rejected) and is never rerun this way. That rerun and the other per-run events and transitions
(run_finished, census_down, census_recovered, _start) are in batch_events.py; this module
re-exports them.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from .batch_events import LATE_START as LATE_START
from .batch_events import _moment as _moment
from .batch_events import _run_event as _run_event
from .batch_events import _signal as _signal
from .batch_events import _spoiled as _spoiled
from .batch_events import _start as _start
from .batch_events import census_down as census_down
from .batch_events import census_recovered as census_recovered
from .batch_events import census_retry as census_retry
from .batch_events import run_finished as run_finished
from .db import Database, mark_closed, setting
from .events import EventRow, emit
from .planparts import TAKING as TAKING
from .planparts import _create_runs, _report_no_node
from .planparts import census_every as census_every
from .planparts import interval_slots as interval_slots
from .planparts import next_slot as next_slot
from .planparts import participants as participants
from .pulse import open_slot
from .references import load_references as load_references
from .sqlrows import _now, _row, _rows

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
PLAN_LOCK = 817452902


# --- planning ----------------------------------------------------------------------------


async def _census_due(
    conn: psycopg.AsyncConnection, now: datetime, lead: timedelta, every: timedelta | None
) -> list[dict]:
    """The census slots in [now - lead - CENSUS_GRACE, now + CENSUS_HORIZON), one per enabled
    country and census target: the daily census_time, or every `every` since the epoch."""
    window = {"now": now, "lo": now - lead - CENSUS_GRACE, "hi": now + CENSUS_HORIZON}
    if every is None:
        # Slots on the previous, current and next UTC day cover a window across midnight.
        return await _rows(
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
    # Every enabled country on the same aligned slots: floor(epoch / every) * every.
    return await _rows(
        conn,
        "SELECT c.country_code,c.region_id,r.slug AS region,t.target_id,s.slot "
        "FROM countries c JOIN runner_regions r ON r.region_id=c.region_id AND r.enabled "
        "CROSS JOIN targets t CROSS JOIN unnest(%(slots)s::timestamptz[]) s(slot) "
        "WHERE c.enabled AND t.census "
        "ORDER BY s.slot,c.country_code,t.sort_order,t.target_id",
        {"slots": interval_slots(every, window["lo"], window["hi"])},
    )


async def _census_slot(
    conn: psycopg.AsyncConnection, slot: dict, lead: timedelta, now: datetime
) -> tuple[str, int | None]:
    """Plan one due census slot. Returns ('exists' or 'deferred', None) when nothing new is
    written, else the new batch's status ('scheduled' or 'skipped') and its id."""
    key = (slot["country_code"], slot["target_id"], slot["slot"])
    if await _row(
        conn,
        "SELECT 1 FROM test_batches WHERE kind='census' AND country_code=%s "
        "AND target_id=%s AND scheduled_for=%s",
        key,
    ):
        return "exists", None
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
        return "deferred", None
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
        return "exists", None
    if previous:
        await _skip_census(conn, inserted["batch_id"], slot, previous, now)
    return status, inserted["batch_id"]


async def _skip_census(
    conn: psycopg.AsyncConnection, batch_id: int, slot: dict, previous: dict, now: datetime
) -> None:
    """Close a census batch planned 'skipped' and put its census_skipped event on the tape."""
    mark_closed(conn, batch_id)
    await emit(
        conn,
        EventRow(
            kind="census_skipped",
            at=now,
            batch_id=batch_id,
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
        ),
    )


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
        if await setting(conn, "pulse.on_demand"):  # its nodes exist only in their pulse hour
            return {"created": 0, "skipped": "on-demand pulses: no census"}
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (PLAN_LOCK,))
        lead = timedelta(seconds=float(await setting(conn, "census.lead_seconds")))
        every = census_every(await setting(conn, "census.every_minutes"))
        products = await participants(conn)
        if not products:
            return {"created": 0, "skipped": 0, "reason": "no registered nodes"}
        due = await _census_due(conn, now, lead, every)
        created, outcomes = [], Counter()
        for slot in due:
            status, batch_id = await _census_slot(conn, slot, lead, now)
            outcomes[status] += 1
            if status == "scheduled":
                created.append(batch_id)
        gaps = await _create_runs(conn, created, now)
        await _report_no_node(conn, gaps, now)
        for batch_id in created:
            await close_batch(conn, batch_id, now)
    return {
        "created": len(created),
        "skipped": outcomes["skipped"],
        "deferred": outcomes["deferred"],
        "no_node": len(gaps),
    }


async def _open_loads(
    conn: psycopg.AsyncConnection,
    references: list[dict],
    target_id: int,
    slot: datetime,
    start_at: datetime,
) -> list[int]:
    """One 'load' batch per region's reference country for a census target's pulse slot."""
    created = []
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
                target_id,
                slot,
                start_at,
            ),
        )
        if load:
            created.append(load["batch_id"])
    return created


async def _close_unmanned(conn: psycopg.AsyncConnection, created: list[int], now: datetime) -> None:
    """Batches whose every run failed (no node in the region) close at once."""
    for row in await _rows(
        conn,
        "SELECT batch_id FROM test_batches b WHERE batch_id=ANY(%s) AND NOT EXISTS "
        "(SELECT 1 FROM batch_runs r WHERE r.batch_id=b.batch_id AND r.status='queued')",
        (created,),
    ):
        await close_batch(conn, row["batch_id"], now)


async def _open_pulses(
    conn: psycopg.AsyncConnection,
    targets: list[dict],
    now: datetime,
    lead: timedelta,
    load_delay: timedelta | None,
) -> tuple[list[int], list[int], int]:
    """Open each due target's pulse slot, and its loads unless load_delay is None (load off).

    The batches created, the targets opened and how many of those batches are loads.
    """
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
        if target["census"] and load_delay is not None:
            if references is None:
                references = await load_references(conn, now)
            load = await _open_loads(
                conn, references, target["target_id"], slot, start + load_delay
            )
            created.extend(load)
            loads += len(load)
    return created, opened, loads


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
        if await setting(conn, "pulse.on_demand"):
            return {"targets": [], "created": 0, "skipped": "on-demand pulses: the hourly job"}
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
        created, opened, loads = await _open_pulses(
            conn, targets, now, lead, delay if loading else None
        )
        gaps = await _create_runs(conn, created, now)
        await _report_no_node(conn, gaps, now)
        await _close_unmanned(conn, created, now)
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
