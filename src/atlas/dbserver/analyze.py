"""`db-server analyze`: ANALYZE the partitioned parents and report one health line.

Autovacuum analyzes partitions but never their partitioned parents, so the planner has no
statistics for a query over a whole parent until someone runs ANALYZE on it. This runs it on
each parent in turn (an ANALYZE of a parent also analyzes its partitions), each in its own
transaction, and times it. A parent that waits longer than the lock timeout for its lock, fails
or is skipped with a WARNING (no MAINTAIN privilege) fails the line; the others still run.

The line also counts the list rows whose place or ASN label is missing (list_row_labels,
store_steps.LABEL_ORPHANS, read-only): since calculated v23 no foreign key refuses them, so one
found fails the line.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import psycopg
from psycopg import sql

from ..calculated.store_steps import LABEL_ORPHANS
from . import connect

PARENTS = ("ip_observations", "run_buckets", "run_geo_buckets", "test_events")
OPTIONAL = ("calculated_snapshot_parts",)  # analyzed once it exists
LOCK_TIMEOUT = "60s"


def analyze(credentials_file, *, lock_timeout: str = LOCK_TIMEOUT) -> dict:
    """The health line: `db_server_analyze` is "ok" or "failed", with each table's status."""
    started = time.monotonic()
    warnings: list[str] = []
    tables = {}
    with connect(credentials_file, autocommit=True) as conn:
        conn.add_notice_handler(
            lambda d: (
                warnings.append(d.message_primary or "")
                if d.severity_nonlocalized == "WARNING"
                else None
            )
        )
        conn.execute("SELECT set_config('lock_timeout', %s, false)", (lock_timeout,))
        for name in PARENTS + OPTIONAL:
            if conn.execute("SELECT to_regclass(%s)", (name,)).fetchone()[0] is None:
                tables[name] = {"status": "absent" if name in OPTIONAL else "missing"}
                continue
            warnings.clear()
            tables[name] = _one(conn, name, warnings)
        labels = _labels(conn)
    healthy = all(t["status"] in ("ok", "absent") for t in (*tables.values(), labels))
    return {
        "db_server_analyze": "ok" if healthy else "failed",
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "seconds": round(time.monotonic() - started, 3),
        "tables": tables,
        "list_row_labels": labels,
    }


def _labels(conn) -> dict:
    """The list rows without their place or ASN label: "ok" with none, "absent" before the
    calculated layer exists, else "failed"."""
    if conn.execute("SELECT to_regclass('calculated_list_rows')").fetchone()[0] is None:
        return {"status": "absent"}
    start = time.monotonic()
    try:
        orphans = conn.execute(LABEL_ORPHANS).fetchone()[0]
    except psycopg.Error as exc:
        result = {"status": "failed", "error": exc.diag.message_primary or type(exc).__name__}
    else:
        result = {"status": "failed" if orphans else "ok", "orphans": orphans}
    return result | {"seconds": round(time.monotonic() - start, 3)}


def _one(conn, name: str, warnings: list[str]) -> dict:
    start = time.monotonic()
    try:
        conn.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(name)))
    except psycopg.Error as exc:
        result = {"status": "failed", "error": exc.diag.message_primary or type(exc).__name__}
    else:
        result = {"status": "warning", "warnings": list(warnings)} if warnings else {"status": "ok"}
    return result | {"seconds": round(time.monotonic() - start, 3)}
