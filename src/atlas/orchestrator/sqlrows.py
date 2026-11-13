"""The shared row helpers of the orchestrator's batch modules: the current time and one or all
rows of a query. The bottom layer: it imports no orchestrator module.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(UTC)


async def _rows(conn: psycopg.AsyncConnection, query: str, params: Any = None) -> list[dict]:
    return await (await conn.execute(query, params)).fetchall()


async def _row(conn: psycopg.AsyncConnection, query: str, params: Any = None) -> dict | None:
    return await (await conn.execute(query, params)).fetchone()
