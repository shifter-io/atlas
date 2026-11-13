"""Operator settings from the command line: the planner switches (atlas scheduler).

Synchronous (psycopg) counterparts of db.setting / db.put_setting for the CLI, which runs
without the orchestrator's async pool. The orchestrator reads the settings table on every
planner tick, so a switch takes effect on the next minute without a restart.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from .. import storage
from .db import DEFAULTS

SWITCHES = {"census": "scheduler.census_enabled", "pulse": "scheduler.pulse_enabled"}


def read_setting(conn: psycopg.Connection, key: str) -> Any:
    # Tuple rows whatever the connection's row factory (the HTML report reads dicts).
    with conn.cursor(row_factory=tuple_row) as cursor:
        row = cursor.execute("SELECT value FROM settings WHERE key=%s", (key,)).fetchone()
    return row[0] if row else DEFAULTS[key]


def write_settings(conn: psycopg.Connection, values: dict[str, Any]) -> None:
    unknown = set(values) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"Unknown settings: {', '.join(sorted(unknown))}")
    for key, value in values.items():
        conn.execute(
            "INSERT INTO settings(key,value) VALUES (%s,%s) ON CONFLICT(key) DO UPDATE "
            "SET value=excluded.value,updated_at=now()",
            (key, Jsonb(value)),
        )


def state(conn: psycopg.Connection) -> dict[str, Any]:
    return {
        **{kind: bool(read_setting(conn, key)) for kind, key in SWITCHES.items()},
        "census_every_minutes": read_setting(conn, "census.every_minutes"),
        "reports_dir": read_setting(conn, "reports.dir"),
    }


def scheduler(credentials_file: str, action: str, kinds: list[str] | None = None) -> dict:
    """`on` / `off` switches the given planners (both by default); `status` only reads."""
    if action not in ("on", "off", "status"):
        raise ValueError("action must be on, off or status")
    kinds = kinds or list(SWITCHES)
    if set(kinds) - set(SWITCHES):
        raise ValueError("kinds must be census and/or pulse")
    with storage.connect(credentials_file) as conn:
        if action != "status":
            write_settings(conn, {SWITCHES[kind]: action == "on" for kind in kinds})
        return state(conn)
