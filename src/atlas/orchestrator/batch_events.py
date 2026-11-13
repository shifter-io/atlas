"""Run events and transitions of census, pulse and load batches.

What happens to one product's run as it starts and ends: census_started (`_start`, which
also starts the batch), census_finished (`run_finished`), the census outage it opens
(`census_down`) or closes (`census_recovered`), and the rerun of a census the provider
spoiled (`census_retry`). Planning and the barrier are in batches.py, which re-exports these
names; the lock order is the same (test_batches row first, then its batch_runs rows).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from .events import EventRow, emit
from .sqlrows import _row, _rows

# A census planned after its start_at (a deferred slot, a restart, a rerun) starts this long
# after now.
LATE_START = timedelta(seconds=5)


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
        EventRow(
            kind="census_finished",
            at=now,
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
        ),
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
            EventRow(
                kind="recovered",
                at=now,
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
                    "duration_s": round(
                        (outage["ended_at"] - outage["started_at"]).total_seconds(), 3
                    ),
                    "outage_batch_id": outage["batch_id"],
                },
            ),
        )
    return len(closed)


async def _open_census_outage(
    conn: psycopg.AsyncConnection, run: dict, batch_id: int, product_id: int, since: datetime
) -> bool:
    """Open the stalled census's outage unless the product has one open in the country."""
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
    return opened is not None


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
    if not await _open_census_outage(conn, run, batch_id, product_id, since):
        return False
    await emit(
        conn,
        EventRow(
            kind="down",
            at=now,
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
        ),
    )
    return True


def _spoiled(run: dict) -> bool:
    """Whether a census run ended because the provider was down: a done run that never got a
    success (no_success) or lost its network mid-run (stalled). A failed run is our side's."""
    return run["status"] == "done" and run["stop_reason"] in ("no_success", "stalled")


async def _retry_blocked(conn: psycopg.AsyncConnection, key: dict, day: datetime) -> bool:
    """Whether the product's census for the country and target was already rerun since `day`
    or a census for the country and target is still open."""
    return bool(
        await _row(
            conn,
            "SELECT 1 FROM test_batches b JOIN batch_runs br USING(batch_id) "
            "WHERE b.kind='census' AND b.retry_of IS NOT NULL AND b.country_code=%(country)s "
            "AND b.target_id=%(target)s AND br.product_id=%(product)s AND b.scheduled_for>=%(day)s",
            key | {"day": day},
        )
        or await _row(
            conn,
            "SELECT 1 FROM test_batches WHERE kind='census' AND country_code=%(country)s "
            "AND target_id=%(target)s AND status IN ('scheduled','running')",
            key,
        )
    )


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
        if await _retry_blocked(conn, key, day):
            continue
        retry_id = await _open_retry(conn, run, census, now)
        if retry_id is not None:
            created.append(retry_id)
    return created


async def _open_retry(
    conn: psycopg.AsyncConnection, run: dict, census: dict, now: datetime
) -> int | None:
    """The rerun of `census` for the product of the pulse `run`, with its census_retry event;
    None when the slot already exists."""
    country = run["country_code"]
    retry = await _row(
        conn,
        "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,"
        "start_at,retry_of) VALUES ('census',%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT DO NOTHING RETURNING batch_id",
        (country, run["region_id"], census["target_id"], now, now + LATE_START, census["batch_id"]),
    )
    if retry is None:
        return None
    # The pulse's node is the product's node in the region.
    await conn.execute(
        "INSERT INTO batch_runs(batch_id,product_id,node_id,status) VALUES (%s,%s,%s,'queued')",
        (retry["batch_id"], run["product_id"], run["node_id"]),
    )
    await emit(
        conn,
        EventRow(
            kind="census_retry",
            at=now,
            batch_id=retry["batch_id"],
            product_id=run["product_id"],
            country_code=country,
            target_id=census["target_id"],
            region_id=run["region_id"],
            data={
                "region": run["region"],
                "retry_of": census["batch_id"],
                "reason": census["stop_reason"] or census["error"],
                "pulse_batch_id": run["batch_id"],
            },
        ),
    )
    return retry["batch_id"]


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
            EventRow(
                kind="census_started",
                at=now,
                batch_id=batch_id,
                product_id=product_id,
                country_code=run["country_code"],
                target_id=run["target_id"],
                region_id=run["region_id"],
                data={
                    "region": run["region"],
                    "start_at": run["start_at"].astimezone(UTC).isoformat(),
                },
            ),
        )
