"""Tape events (test_events) and operator alerts (alerts).

test_events is partitioned by month on `at`: callers create the partition with
`Database.ensure_partition("test_events", at)` before the transaction that inserts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

SEVERITIES = ("info", "minor", "major", "critical")


async def emit(
    conn: psycopg.AsyncConnection,
    kind: str,
    at: datetime,
    *,
    batch_id: int | None = None,
    product_id: int | None = None,
    country_code: str | None = None,
    target_id: str | None = None,
    region_id: int | None = None,
    signal: int = 0,
    severity: str = "info",
    data: dict[str, Any] | None = None,
) -> int:
    row = await (
        await conn.execute(
            "INSERT INTO test_events(at,kind,batch_id,product_id,country_code,target_id,"
            "region_id,signal,severity,data) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "RETURNING event_id",
            (
                at,
                kind,
                batch_id,
                product_id,
                country_code,
                target_id,
                region_id,
                signal,
                severity,
                Jsonb(data or {}),
            ),
        )
    ).fetchone()
    return row["event_id"]


async def raise_alert(
    conn: psycopg.AsyncConnection,
    kind: str,
    message: str,
    *,
    severity: str = "minor",
    dedupe_key: str | None = None,
    data: dict[str, Any] | None = None,
) -> bool:
    """Open an alert, or bump the open one with the same dedupe key.

    An acknowledged alert that fires again is reopened. Returns True when the alert is new or
    reopened, which callers use to write a tape event once per incident rather than per tick.
    Run inside a transaction: the prior row is locked while it is read.
    """
    if severity not in SEVERITIES:
        raise ValueError("Unknown alert severity")
    key = dedupe_key or kind
    prior = await (
        await conn.execute(
            "SELECT acknowledged_at FROM alerts WHERE dedupe_key=%s FOR UPDATE", (key,)
        )
    ).fetchone()
    await conn.execute(
        "INSERT INTO alerts(kind,severity,message,data,dedupe_key) VALUES (%s,%s,%s,%s,%s) "
        "ON CONFLICT(dedupe_key) DO UPDATE SET occurrences=alerts.occurrences+1,last_at=now(),"
        "message=excluded.message,severity=excluded.severity,data=excluded.data,"
        "acknowledged_at=NULL,first_at=CASE WHEN alerts.acknowledged_at IS NULL "
        "THEN alerts.first_at ELSE now() END",
        (kind, severity, message, Jsonb(data or {}), key),
    )
    return prior is None or prior["acknowledged_at"] is not None
