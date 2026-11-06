"""The admin UI's writers: kill or skip a batch, acknowledge an alert, edit a country or target.

Each takes an already validated value; `routes.py` owns the parsing and the HTTP errors. The
query helpers are shared with the readers in `views.py`, which never imports this module.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any

import psycopg

from ..batches import run_finished
from ..db import Database, mark_closed
from ..events import EventRow, emit
from .views import _hhmm, _now, _row, _rows

OPEN_BATCHES = ("scheduled", "running")
# Editable per-target schedule fields and their inclusive bounds.
TARGET_FIELDS: dict[str, tuple[int, int] | None] = {
    "pulse_enabled": None,
    "pulse_interval_minutes": (1, 1440),
    "pulse_requests": (1, 100_000),
    "pulse_concurrency": (1, 10_000),
    "census_concurrency": (1, 100_000),
}


class Conflict(Exception):
    """The action does not apply to the row in its current state."""


async def stop_batch(
    db: Database, batch_id: int, status: str, now: datetime | None = None
) -> dict[str, Any] | None:
    """Kill or skip an open batch: the batch and its open runs end now, nodes are told to stop.

    `status` is 'killed' or 'skipped'. Open runs become 'killed' with stop_reason 'operator'
    either way (a skipped batch still stops what is already running); runs that already
    finished keep their result. Each killed census run gets its census_finished event from
    batches.run_finished, like any other run that ends. On top of that a skipped census writes
    'census_skipped', and a killed or skipped pulse batch 'pulse_finished' (pulse runs write no
    per-run event), both with data.reason 'operator'.
    Returns None for an unknown batch; raises Conflict when it is no longer open.
    """
    if status not in ("killed", "skipped"):
        raise ValueError("status must be killed or skipped")
    now = _now(now)
    await db.ensure_partition("test_events", now)
    async with db.transaction() as conn:
        batch = await _row(
            conn,
            "SELECT b.batch_id,b.kind,b.status,b.country_code,b.target_id,b.region_id,"
            "r.slug AS region FROM test_batches b JOIN runner_regions r "
            "ON r.region_id=b.region_id WHERE b.batch_id=%s FOR UPDATE OF b",
            (batch_id,),
        )
        if batch is None:
            return None
        if batch["status"] not in OPEN_BATCHES:
            raise Conflict(f"batch {batch_id} is already {batch['status']}")
        killed = await _rows(
            conn,
            "UPDATE batch_runs SET status='killed',finished_at=%s,stop_reason='operator' "
            "WHERE batch_id=%s AND status IN ('queued','running','uploading') "
            "RETURNING product_id",
            (now, batch_id),
        )
        await conn.execute(
            "UPDATE test_batches SET status=%s,finished_at=%s WHERE batch_id=%s",
            (status, now, batch_id),
        )
        mark_closed(conn, batch_id)
        for run in killed:
            await run_finished(conn, batch_id, run["product_id"], now)
        # Load batches (latency under load) have no tape event of their own.
        if batch["kind"] != "load" and (batch["kind"] == "pulse" or status == "skipped"):
            totals = await _row(
                conn,
                "SELECT coalesce(sum(requests),0)::bigint AS requests,"
                "coalesce(sum(successes),0)::bigint AS successes,"
                "coalesce(sum(unique_ips),0)::bigint AS unique_ips "
                "FROM batch_runs WHERE batch_id=%s",
                (batch_id,),
            )
            await emit(
                conn,
                EventRow(
                    kind="census_skipped" if batch["kind"] == "census" else "pulse_finished",
                    at=now,
                    batch_id=batch_id,
                    country_code=batch["country_code"],
                    target_id=batch["target_id"],
                    region_id=batch["region_id"],
                    severity="minor",
                    data={
                        "region": batch["region"],
                        "reason": "operator",
                        "status": status,
                        "runs_killed": len(killed),
                        **totals,
                    },
                ),
            )
    return {"batch_id": batch_id, "status": status, "runs_killed": len(killed)}


async def acknowledge(conn: psycopg.AsyncConnection, alert_id: int) -> bool | None:
    """True when acknowledged now, False when it already was, None for an unknown alert."""
    if await _row(
        conn,
        "UPDATE alerts SET acknowledged_at=now() WHERE alert_id=%s AND acknowledged_at IS NULL "
        "RETURNING alert_id",
        (alert_id,),
    ):
        return True
    exists = await _row(conn, "SELECT 1 FROM alerts WHERE alert_id=%s", (alert_id,))
    return False if exists else None


async def set_country(
    conn: psycopg.AsyncConnection,
    country_code: str,
    *,
    census_time: time | None = None,
    enabled: bool | None = None,
) -> dict[str, Any] | None:
    """Change a country's census slot and/or enabled flag. None for an unknown country."""
    row = await _row(
        conn,
        "UPDATE countries SET census_time=coalesce(%s,census_time),enabled=coalesce(%s,enabled) "
        "WHERE country_code=%s RETURNING country_code,census_time,enabled",
        (census_time, enabled, country_code),
    )
    if row is not None:
        row["census_time"] = _hhmm(row["census_time"])
    return row


async def set_target(
    conn: psycopg.AsyncConnection, target_id: str, values: dict[str, Any]
) -> dict[str, Any] | None:
    """Change a target's pulse and census settings (keys from TARGET_FIELDS, pre-validated)."""
    unknown = set(values) - set(TARGET_FIELDS)
    if unknown or not values:
        raise ValueError("unknown or missing target fields")
    assignments = ",".join(f"{key}=%({key})s" for key in TARGET_FIELDS if key in values)
    return await _row(
        conn,
        f"UPDATE targets SET {assignments} WHERE target_id=%(target_id)s RETURNING target_id,"
        "census,census_concurrency,pulse_enabled,pulse_interval_minutes,pulse_requests,"
        "pulse_concurrency",
        values | {"target_id": target_id},
    )
