"""Shared helpers of the ESTIMATE-1 tests (test_estimates*.py): the formula, the windows and
the imported runs with their stored hours."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from psycopg.rows import dict_row
from test_storage import measurement

from proxy_index.estimates import compute
from proxy_index.orchestrator.db import Database
from proxy_index.storage import connect, ingest

POLICY = Path("configs/methodology.toml")
T1 = datetime(2026, 10, 1, 15, 20, tzinfo=UTC)
T2 = T1 + timedelta(days=1)


def formula(version=1, **countries):
    """A small formula: hour factors 1..24 / 24 (none at hour 3), daily factor 3."""
    factors = [(h + 1) / 24 for h in range(24)]
    factors[3] = None
    entry = {"curve": "country", "hour_factors": factors, "daily_factor": 3.0}
    entry["requests"] = [100 * (h + 1) for h in range(24)]
    return {
        "schema": compute.SCHEMA,
        "schema_version": 1,
        "formula_version": version,
        "network_curve": {"requests": [50] * 24},
        "countries": {code: entry | extra for code, extra in countries.items()},
    }


def product_window(start):
    return {
        "period": {"start": start},
        "pool": {"unique_ips": 4, "datacenter_asn_share": 0.25},
        "overlap": {"shared_share": 0.5},
    }


async def imported(tmp_path, database, started_at=None):
    """One imported fixture run (1 IP, 3 requests) measured at started_at; returns its id."""
    directory = await measurement(tmp_path / f"run{len(list(tmp_path.glob('run*')))}")
    ingest(directory, database)
    run_id = json.loads((directory / "summary.json").read_text())["run_id"]
    if started_at:
        with connect(database) as conn:
            conn.execute("UPDATE test_runs SET started_at=%s WHERE run_id=%s", (started_at, run_id))
    return run_id


def stored(database):
    with connect(database) as conn:
        conn.row_factory = dict_row
        return conn.execute(
            "SELECT day,hour,unique_ips,requests,day_unique_ips,formula_version,released_at,"
            "measured_at FROM estimated_hours ORDER BY day,hour"
        ).fetchall()


@pytest.fixture
async def db(database):
    pool = Database(database)
    yield pool
    await pool.close()
