"""Census and pulse batches: planning, the per-provider runs, the barrier and the reaper.

A test_batches row is one synchronized slot (kind x country x target x scheduled_for) that every
participating product runs from its node in the country's region, starting at the same
`start_at`. Each product's part is a batch_runs row. A product participates when it has at
least one registered node anywhere; a participating product without a node in the batch's
region gets a 'failed' run with stop_reason/error 'no_node' (plus an alert, and a 'no_node'
tape event when that alert opens).

Lock order, everywhere: test_batches row (FOR UPDATE) first, then its batch_runs rows. The
barrier (`close_batch`) runs under the batch lock: a batch is 'done' once every run is terminal
and at least one finished, 'failed' when none did.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from .db import Database, setting
from .events import emit, raise_alert

log = logging.getLogger(__name__)

TERMINAL_RUNS = ("done", "failed", "skipped", "killed")
OPEN_BATCHES = ("scheduled", "running")
COUNTERS = (
    "requests",
    "successes",
    "unique_ips",
    "challenges",
    "blocks",
    "timeouts",
    "proxy_errors",
    "bytes_up",
    "bytes_down",
)
# Census slots are matched in [now - CENSUS_GRACE, now + CENSUS_HORIZON). The minute-aligned
# planner covers every slot twice, so a late or missed tick (restart) still plans it; the
# UNIQUE(kind, country, target, scheduled_for) key keeps that idempotent.
CENSUS_HORIZON = timedelta(seconds=60)
CENSUS_GRACE = timedelta(seconds=60)
# Nodes are handed runs this far ahead of start_at so they can wait on the wall clock.
ASSIGN_AHEAD = timedelta(minutes=5)
PLAN_LOCK = 817452902
# An upload is being received or ingested (a large census takes minutes): allow it longer.
UPLOAD_STALE = 3


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(UTC)


async def _rows(conn: psycopg.AsyncConnection, query: str, params: Any = None) -> list[dict]:
    return await (await conn.execute(query, params)).fetchall()


async def _row(conn: psycopg.AsyncConnection, query: str, params: Any = None) -> dict | None:
    return await (await conn.execute(query, params)).fetchone()


# --- planning ----------------------------------------------------------------------------


async def participants(conn: psycopg.AsyncConnection) -> list[dict]:
    """Products with at least one registered node, in any region."""
    return await _rows(
        conn,
        "SELECT DISTINCT p.product_id,p.provider,p.name FROM nodes n JOIN products p "
        "ON p.provider=n.provider AND p.name=n.product ORDER BY p.product_id",
    )


async def _create_runs(
    conn: psycopg.AsyncConnection, batch_ids: list[int], now: datetime
) -> list[dict]:
    """One batch_runs row per participating product; returns the no_node gaps per region."""
    if not batch_ids:
        return []
    await conn.execute(
        "WITH taking AS (SELECT DISTINCT p.product_id,p.provider,p.name FROM nodes n "
        " JOIN products p ON p.provider=n.provider AND p.name=n.product) "
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


async def census_plan(db: Database, now: datetime | None = None) -> dict[str, Any]:
    """Create the census batches for every enabled country whose daily slot is due.

    One batch per census target, start_at = slot + census.lead_seconds. A country whose
    previous census (same target) is still scheduled or running gets its new batch 'skipped'
    and a 'census_skipped' event instead.
    """
    now = _now(now)
    await db.ensure_partition("test_events", now)
    async with db.transaction() as conn:
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (PLAN_LOCK,))
        lead = timedelta(seconds=float(await setting(conn, "census.lead_seconds")))
        products = await participants(conn)
        if not products:
            return {"created": 0, "skipped": 0, "reason": "no registered nodes"}
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
            {"now": now, "lo": now - CENSUS_GRACE, "hi": now + CENSUS_HORIZON},
        )
        created, skipped = [], 0
        for slot in due:
            previous = await _row(
                conn,
                "SELECT batch_id,status,scheduled_for FROM test_batches WHERE kind='census' "
                "AND country_code=%s AND target_id=%s AND status IN ('scheduled','running') "
                "AND scheduled_for<>%s ORDER BY scheduled_for DESC LIMIT 1",
                (slot["country_code"], slot["target_id"], slot["slot"]),
            )
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
                    slot["slot"] + lead,
                    status,
                    now if previous else None,
                ),
            )
            if inserted is None:
                continue
            if previous:
                skipped += 1
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
    return {"created": len(created), "skipped": skipped, "no_node": len(gaps)}


async def pulse_plan(db: Database, now: datetime | None = None) -> dict[str, Any]:
    """Open the current slot of every pulse-enabled target that has no batch for it yet.

    Slots are aligned to the target's interval since the epoch (floor(now / interval)); a due
    target gets one batch per enabled country, all with the same start_at. Targets added to
    the table are picked up on the next tick.
    """
    now = _now(now)
    await db.ensure_partition("test_events", now)
    async with db.transaction() as conn:
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (PLAN_LOCK,))
        lead = timedelta(seconds=float(await setting(conn, "pulse.lead_seconds")))
        if not await participants(conn):
            return {"targets": 0, "created": 0, "reason": "no registered nodes"}
        targets = await _rows(
            conn,
            "SELECT target_id,pulse_interval_minutes FROM targets WHERE pulse_enabled "
            "ORDER BY sort_order,target_id",
        )
        created: list[int] = []
        opened = []
        for target in targets:
            every = target["pulse_interval_minutes"] * 60
            slot = datetime.fromtimestamp(now.timestamp() // every * every, UTC)
            if await _row(
                conn,
                "SELECT 1 FROM test_batches WHERE kind='pulse' AND target_id=%s "
                "AND scheduled_for=%s LIMIT 1",
                (target["target_id"], slot),
            ):
                continue
            rows = await _rows(
                conn,
                "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,"
                "start_at) SELECT 'pulse',c.country_code,c.region_id,%s,%s,%s FROM countries c "
                "JOIN runner_regions r ON r.region_id=c.region_id AND r.enabled WHERE c.enabled "
                "ORDER BY c.country_code ON CONFLICT DO NOTHING RETURNING batch_id",
                (target["target_id"], slot, max(slot, now) + lead),
            )
            created.extend(row["batch_id"] for row in rows)
            opened.append(target["target_id"])
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
    return {"targets": opened, "created": len(created), "no_node": len(gaps)}


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


# --- node-facing transitions ---------------------------------------------------------------


async def assignments(db: Database, node: dict, now: datetime | None = None) -> dict[str, Any]:
    """Hand a node its queued runs due within ASSIGN_AHEAD (marking them 'running'), and the
    batches it must stop. A run handed out but not yet started is handed out again, so a node
    that restarted while waiting for start_at does not lose it."""
    now = _now(now)
    async with db.transaction() as conn:
        min_sample = int(await setting(conn, "census.min_sample"))
        stall = float(await setting(conn, "census.stall_seconds"))
        stall_seconds = int(stall) if stall.is_integer() else stall
        rows = await _rows(
            conn,
            "SELECT b.batch_id,b.kind,b.country_code,b.target_id,b.start_at,r.slug AS region,"
            " br.product_id,p.provider,p.name,t.url,t.timeout_ms,t.census_concurrency,"
            " t.pulse_requests,t.pulse_concurrency "
            "FROM batch_runs br JOIN test_batches b USING(batch_id) "
            "JOIN products p ON p.product_id=br.product_id "
            "JOIN targets t ON t.target_id=b.target_id "
            "JOIN runner_regions r ON r.region_id=b.region_id "
            "WHERE br.node_id=%s AND b.status IN ('scheduled','running') "
            " AND (br.status='queued' OR (br.status='running' AND br.started_at IS NULL)) "
            " AND b.start_at<=%s ORDER BY b.start_at,b.batch_id FOR UPDATE OF br SKIP LOCKED",
            (node["node_id"], now + ASSIGN_AHEAD),
        )
        if rows:
            await conn.execute(
                "UPDATE batch_runs SET status='running' WHERE node_id=%s AND batch_id=ANY(%s) "
                "AND status='queued'",
                (node["node_id"], [row["batch_id"] for row in rows]),
            )
        kill = await _rows(
            conn,
            "SELECT br.batch_id FROM batch_runs br JOIN test_batches b USING(batch_id) "
            "WHERE br.node_id=%s AND (br.status IN ('killed','skipped') "
            " OR b.status IN ('killed','skipped')) "
            "AND coalesce(br.finished_at,b.finished_at,%s)>%s ORDER BY br.batch_id",
            (node["node_id"], now, now - timedelta(minutes=15)),
        )
    work = []
    for row in rows:
        census = row["kind"] == "census"
        work.append(
            {
                "batch_id": row["batch_id"],
                "kind": row["kind"],
                "product_id": row["product_id"],
                "provider": row["provider"],
                "product": row["name"],
                "country_code": row["country_code"].strip(),
                "region": row["region"],
                "target_id": row["target_id"],
                "target_url": row["url"] or None,
                "timeout_ms": row["timeout_ms"],
                "concurrency": row["census_concurrency"] if census else row["pulse_concurrency"],
                "stop_rule": "unique_ratio" if census else "count",
                "requests": None if census else row["pulse_requests"],
                "min_sample": min_sample if census else None,
                "stall_seconds": stall_seconds if census else None,
                "start_at": row["start_at"].astimezone(UTC).isoformat(),
            }
        )
    return {"assignments": work, "kill": [row["batch_id"] for row in kill]}


def _counters(values: dict[str, Any]) -> dict[str, int]:
    result = {}
    for key in COUNTERS:
        value = values.get(key, 0)
        if value is None:
            value = 0
        if type(value) is not int or value < 0:
            raise ValueError(f"Counter {key} must be a non-negative integer")
        result[key] = value
    return result


async def _run(conn: psycopg.AsyncConnection, node: dict, batch_id: int) -> dict | None:
    return await _row(
        conn,
        "SELECT br.*,b.status AS batch_status,b.kind,b.target_id FROM batch_runs br "
        "JOIN test_batches b USING(batch_id) WHERE br.batch_id=%s AND br.node_id=%s",
        (batch_id, node["node_id"]),
    )


async def progress(
    db: Database, node: dict, batch_id: int, counters: dict[str, Any], now: datetime | None = None
) -> dict[str, Any] | None:
    """Live counters. Returns None for an unknown run, else {"stop": bool}: a run that was
    killed, skipped or failed by the reaper must stop."""
    now = _now(now)
    values = _counters(counters)
    await db.ensure_partition("test_events", now)
    async with db.transaction() as conn:
        run = await _run(conn, node, batch_id)
        if run is None:
            return None
        if run["started_at"] is None and run["status"] == "running":
            await _start(conn, batch_id, run["product_id"], now)
        updated = await _row(
            conn,
            "UPDATE batch_runs SET last_progress_at=%(now)s,"
            + ",".join(f"{key}=%({key})s" for key in COUNTERS)
            + " WHERE batch_id=%(batch)s AND product_id=%(product)s AND status='running' "
            "RETURNING status",
            values | {"now": now, "batch": batch_id, "product": run["product_id"]},
        )
    stop = updated is None or run["batch_status"] in ("killed", "skipped", "failed")
    return {"stop": stop}


async def begin_upload(db: Database, node: dict, batch_id: int, now: datetime | None = None):
    """Mark a run 'uploading'. Returns the run row as it was, or None when unknown."""
    now = _now(now)
    async with db.transaction() as conn:
        run = await _run(conn, node, batch_id)
        if run is not None and run["status"] in ("running", "uploading"):
            await conn.execute(
                "UPDATE batch_runs SET status='uploading',last_progress_at=%s,"
                "started_at=coalesce(started_at,%s) WHERE batch_id=%s AND product_id=%s",
                (now, now, batch_id, run["product_id"]),
            )
        return run


async def finish_run(
    db: Database,
    node: dict,
    batch_id: int,
    summary: dict[str, Any],
    run_key: int,
    now: datetime | None = None,
) -> str:
    """Link the ingested run and take its final counters from the summary; then the barrier."""
    now = _now(now)
    completed = summary.get("status") == "completed"
    values = {
        "requests": summary.get("completed") or 0,
        "successes": summary.get("successes") or 0,
        "unique_ips": summary.get("unique_ips") or 0,
        **{key: summary.get(key) or 0 for key in COUNTERS[3:]},
    }
    await db.ensure_partition("test_events", now)
    async with db.transaction() as conn:
        await _row(
            conn, "SELECT 1 FROM test_batches WHERE batch_id=%s FOR UPDATE", (batch_id,)
        )
        updated = await _row(
            conn,
            "UPDATE batch_runs SET status=%(status)s,run_key=%(run_key)s,finished_at=%(now)s,"
            "last_progress_at=%(now)s,started_at=coalesce(started_at,%(started)s),"
            "stop_reason=%(stop_reason)s,error=%(error)s,"
            + ",".join(f"{key}=%({key})s" for key in COUNTERS)
            + " WHERE batch_id=%(batch)s AND node_id=%(node)s "
            "AND status IN ('running','uploading') RETURNING status,product_id",
            values
            | {
                "status": "done" if completed else "failed",
                "run_key": run_key,
                "now": now,
                "started": now,
                "stop_reason": summary.get("stop_reason"),
                "error": None if completed else f"run {summary.get('status')}",
                "batch": batch_id,
                "node": node["node_id"],
            },
        )
        if updated is None:
            run = await _run(conn, node, batch_id)
            return run["status"] if run else "missing"
        product_id = updated["product_id"]
        await census_recovered(conn, batch_id, product_id, summary, now)
        await run_finished(conn, batch_id, product_id, now)
        await census_down(conn, batch_id, product_id, summary, now)
        await close_batch(conn, batch_id, now)
        return updated["status"]


async def fail_run(
    db: Database, node: dict, batch_id: int, error: str, now: datetime | None = None
) -> str | None:
    now = _now(now)
    await db.ensure_partition("test_events", now)
    async with db.transaction() as conn:
        await _row(
            conn, "SELECT 1 FROM test_batches WHERE batch_id=%s FOR UPDATE", (batch_id,)
        )
        run = await _row(
            conn,
            "UPDATE batch_runs SET status='failed',error=%s,finished_at=%s,"
            "stop_reason=coalesce(stop_reason,'error') WHERE batch_id=%s AND node_id=%s "
            "AND status NOT IN ('done','failed','skipped','killed') RETURNING product_id",
            (error[:500], now, batch_id, node["node_id"]),
        )
        if run is None:
            existing = await _run(conn, node, batch_id)
            return existing["status"] if existing else None
        await run_finished(conn, batch_id, run["product_id"], now)
        await close_batch(conn, batch_id, now)
        return "failed"


# --- reaper ------------------------------------------------------------------------------


async def reap(db: Database, now: datetime | None = None) -> dict[str, Any]:
    """Fail runs that never started or went silent, then close what that completes."""
    now = _now(now)
    await db.ensure_partition("test_events", now)
    async with db.connection() as conn:
        grace = timedelta(seconds=float(await setting(conn, "batch.start_grace_seconds")))
        stale = timedelta(seconds=float(await setting(conn, "batch.stale_seconds")))
        candidates = await _rows(
            conn,
            "SELECT DISTINCT b.batch_id FROM test_batches b LEFT JOIN batch_runs br "
            "USING(batch_id) WHERE b.status IN ('scheduled','running') AND ("
            " br.batch_id IS NULL OR br.status IN ('done','failed','skipped','killed')"
            " OR (br.started_at IS NULL AND b.start_at<%(late)s)"
            " OR (br.started_at IS NOT NULL AND br.status='running'"
            "  AND coalesce(br.last_progress_at,br.started_at)<%(stale)s)"
            " OR (br.status='uploading' AND br.last_progress_at<%(ingest)s)) ORDER BY b.batch_id",
            {"late": now - grace, "stale": now - stale, "ingest": now - UPLOAD_STALE * stale},
        )
    failed: Counter[str] = Counter()
    closed = 0
    for candidate in candidates:
        batch_id = candidate["batch_id"]
        async with db.transaction() as conn:
            batch = await _row(
                conn,
                "SELECT status FROM test_batches WHERE batch_id=%s FOR UPDATE",
                (batch_id,),
            )
            if batch["status"] not in OPEN_BATCHES:
                continue
            reaped = await _rows(
                conn,
                "UPDATE batch_runs br SET status='failed',finished_at=%(now)s,"
                "stop_reason=coalesce(stop_reason,'error'),"
                "error=CASE WHEN br.started_at IS NULL THEN 'not_started' ELSE 'stale' END "
                "FROM test_batches b WHERE b.batch_id=br.batch_id AND br.batch_id=%(batch)s AND ("
                " (br.started_at IS NULL AND br.status IN ('queued','running')"
                "  AND b.start_at<%(late)s)"
                " OR (br.started_at IS NOT NULL AND br.status='running'"
                "  AND coalesce(br.last_progress_at,br.started_at)<%(stale)s)"
                " OR (br.status='uploading' AND br.last_progress_at<%(ingest)s)) "
                "RETURNING br.product_id,br.node_id,br.error",
                {"now": now, "batch": batch_id, "late": now - grace, "stale": now - stale,
                 "ingest": now - UPLOAD_STALE * stale},
            )
            for run in reaped:
                failed[run["error"]] += 1
                await run_finished(conn, batch_id, run["product_id"], now)
                await raise_alert(
                    conn,
                    "run_" + run["error"],
                    f"node {run['node_id']} left a run {run['error'].replace('_', ' ')}",
                    severity="minor",
                    dedupe_key=f"run_{run['error']}:{run['node_id']}",
                    data={"batch_id": batch_id, "node_id": run["node_id"]},
                )
            if await close_batch(conn, batch_id, now) not in OPEN_BATCHES:
                closed += 1
    return {"failed": dict(failed), "closed": closed}


async def mark_offline(db: Database, now: datetime | None = None) -> dict[str, Any]:
    """Nodes without a heartbeat for node.offline_seconds go offline: alert and tape event."""
    now = _now(now)
    await db.ensure_partition("test_events", now)
    async with db.transaction() as conn:
        limit = timedelta(seconds=float(await setting(conn, "node.offline_seconds")))
        gone = await _rows(
            conn,
            "UPDATE nodes n SET status='offline' FROM runner_regions r "
            "WHERE r.region_id=n.region_id AND n.status<>'offline' "
            "AND (n.last_heartbeat IS NULL OR n.last_heartbeat<%s) "
            "RETURNING n.node_id,n.region_id,r.slug AS region,n.provider,n.product,n.host,"
            "n.last_heartbeat,(SELECT product_id FROM products p WHERE p.provider=n.provider "
            "AND p.name=n.product) AS product_id",
            (now - limit,),
        )
        for node in gone:
            data = {
                "region": node["region"],
                "provider": node["provider"],
                "product": f"{node['provider']}/{node['product']}",
                "node_id": node["node_id"],
                "host": node["host"],
                "last_heartbeat": node["last_heartbeat"].astimezone(UTC).isoformat()
                if node["last_heartbeat"]
                else None,
            }
            await raise_alert(
                conn,
                "node_offline",
                f"node {node['node_id']} ({data['product']} in {node['region']}) is offline",
                severity="major",
                dedupe_key=f"node_offline:{node['node_id']}",
                data=data,
            )
            await emit(
                conn,
                "node_offline",
                now,
                product_id=node["product_id"],
                region_id=node["region_id"],
                signal=-1,
                severity="major",
                data=data,
            )
    return {"offline": [node["node_id"] for node in gone]}


async def online(conn: psycopg.AsyncConnection, node_id: int, values: dict[str, Any]) -> None:
    """Heartbeat or registration: the node is online, and its offline alert resolves."""
    assignments = ["status='online'", "last_heartbeat=now()"]
    assignments += [f"{key}=%({key})s" for key in values]
    previous = await _row(
        conn,
        # The self-join reads the row as it was before this update.
        "UPDATE nodes n SET " + ",".join(assignments) + " FROM nodes old "
        "WHERE n.node_id=%(node)s AND old.node_id=n.node_id RETURNING old.status",
        values | {"node": node_id},
    )
    if previous and previous["status"] == "offline":
        await conn.execute(
            "UPDATE alerts SET acknowledged_at=now() WHERE dedupe_key=%s "
            "AND acknowledged_at IS NULL",
            (f"node_offline:{node_id}",),
        )
