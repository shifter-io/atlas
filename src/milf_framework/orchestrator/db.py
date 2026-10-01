"""A small async connection pool over psycopg, and the settings and partition helpers.

The framework has no pool dependency; the orchestrator's traffic (a heartbeat every ten seconds
and a progress post every thirty per node, plus the jobs) needs only a handful of connections.
Connections are autocommit; writes that must agree run inside `transaction()`.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg import pq
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .. import storage
from ..providers.credentials import read_credentials

DEFAULTS: dict[str, Any] = {
    # Seconds between a census slot and the synchronized start every node waits for.
    "census.lead_seconds": 30,
    # Successes before the unique-ratio rule is evaluated (RunConfig.min_sample).
    "census.min_sample": 1000,
    # A census that had successes and then none for this long stops as 'stalled' (an outage).
    "census.stall_seconds": 600,
    # Seconds between a pulse slot opening and its synchronized start.
    "pulse.lead_seconds": 15,
    # A queued run whose start_at passed this long ago without the node starting it fails.
    "batch.start_grace_seconds": 180,
    # A running or uploading run silent for this long fails; nodes post progress every 30 s.
    "batch.stale_seconds": 900,
    "node.offline_seconds": 60,
    "calculate.min_interval_seconds": 300,
    "retention.events_days": 90,
    "retention.job_runs_days": 30,
    # Upload limits for /v1/results.
    "results.max_upload_bytes": 1 << 30,
    "results.max_extracted_bytes": 4 << 30,
}


class Database:
    """Reusable autocommit connections, at most `size` at once."""

    def __init__(self, credentials_file: str, *, size: int = 8) -> None:
        credentials = read_credentials(credentials_file, "postgres")
        if set(credentials) - {"host", "port", "dbname", "user", "password", "sslmode"}:
            raise ValueError("Unsupported database connection field")
        self.credentials_file = str(credentials_file)
        self._credentials = credentials
        self._idle: list[psycopg.AsyncConnection] = []
        self._slots = asyncio.Semaphore(size)
        self._partitions: set[str] = set()

    async def _open(self) -> psycopg.AsyncConnection:
        return await psycopg.AsyncConnection.connect(
            **self._credentials, autocommit=True, connect_timeout=5, row_factory=dict_row
        )

    @contextlib.asynccontextmanager
    async def connection(self) -> AsyncIterator[psycopg.AsyncConnection]:
        async with self._slots:
            conn = self._idle.pop() if self._idle else await self._open()
            reusable = False
            try:
                yield conn
                reusable = True
            finally:
                if (
                    reusable
                    and not conn.closed
                    and not conn.broken
                    and conn.info.transaction_status == pq.TransactionStatus.IDLE
                ):
                    self._idle.append(conn)
                else:
                    await conn.close()

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[psycopg.AsyncConnection]:
        async with self.connection() as conn, conn.transaction():
            yield conn

    async def close(self) -> None:
        idle, self._idle = self._idle, []
        for conn in idle:
            await conn.close()

    async def ensure_partition(self, table: str, moment: datetime) -> None:
        """storage.partition on its own autocommit connection (same names and bounds), so a
        transaction that rolls back cannot leave the cache claiming a partition exists."""
        captured: list[Any] = []

        class Capture:
            def execute(self, query: Any) -> None:
                captured.append(query)

        storage.partition(Capture(), table, moment)
        key = f"{table}:{moment.astimezone(UTC):%Y-%m}"
        if key in self._partitions:
            return
        async with self.connection() as conn:
            for query in captured:
                await conn.execute(query)
        self._partitions.add(key)

async def setting(conn: psycopg.AsyncConnection, key: str) -> Any:
    """An operator setting from the settings table, else the built-in default."""
    row = await (await conn.execute("SELECT value FROM settings WHERE key=%s", (key,))).fetchone()
    return row["value"] if row else DEFAULTS[key]


async def put_setting(conn: psycopg.AsyncConnection, key: str, value: Any) -> None:
    await conn.execute(
        "INSERT INTO settings(key,value) VALUES (%s,%s) ON CONFLICT(key) DO UPDATE "
        "SET value=excluded.value,updated_at=now()",
        (key, Jsonb(value)),
    )
