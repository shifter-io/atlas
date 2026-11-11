"""TAPE-STATS-1's measurement: what the test_event_buckets trigger adds to a daily campaign hour's
event inserts (INGEST-BATCH-2's path, daily/ingest.emit_cell) through a delayed link.

Each round emits `--cells` cells' daily_test_started and daily_test_finished, one transaction per
cell as emit_cell does, on a fresh database through ingest_cases.Proxy: once with the trigger
(schema v13), once with it dropped (the v12 cost). The order alternates by round. On a
disposable server with PG_TEST_ADMIN_FILE set:
    python tests/event_buckets_measure.py --delay 0.039 [--cells 72] [--rounds 5]
prints one JSON line per round and mode (wall time, statements, round trips), then the medians
and the trigger's share of the wall time.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import psycopg  # noqa: E402
from ingest_cases import Proxy, credentials_file, proxied  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from proxy_index import storage  # noqa: E402
from proxy_index.orchestrator import events  # noqa: E402
from proxy_index.partitions import partition  # noqa: E402
from proxy_index.providers.credentials import read_credentials  # noqa: E402

HOUR = datetime(2026, 11, 9, 14, tzinfo=UTC)
SETUP = (
    "INSERT INTO providers VALUES ('fixture');"
    "INSERT INTO products(provider,name) VALUES ('fixture','residential');"
    "INSERT INTO runner_regions(region_id,slug,pattern) VALUES (1,'fra1','fra1-*')"
)


def prepare(direct: Path, trigger: bool) -> int:
    """A fresh v13 database's product id; without trigger, the v12 cost (trigger dropped)."""
    storage.initialize(direct)
    with storage.connect(direct) as conn:
        conn.execute(SETUP)
        partition(conn, "test_events", HOUR)
        if not trigger:
            conn.execute("DROP TRIGGER test_event_buckets_add ON test_events")
        return conn.execute("SELECT product_id FROM products").fetchone()[0]


async def emit_hour(conninfo: str, product_id: int, cells: int) -> None:
    """The hour's cells as emit_cell writes them: one transaction of two inserts each."""
    async with await psycopg.AsyncConnection.connect(conninfo, row_factory=dict_row) as conn:
        cell = {"product_id": product_id, "region_id": 1, "region": "fra1", "countries": ("US",)}
        test = events.DailyTest(**cell)
        result = events.DailyTestResult(status="done", requests=100, unique_ips=50, duration_s=60)
        for n in range(cells):
            began = HOUR + timedelta(seconds=n)
            async with conn.transaction():
                await events.emit_daily_test_started(conn, began, test)
                ended = began + timedelta(minutes=1)
                await events.emit_daily_test_finished(conn, ended, test, result)


def conninfo(credentials: Path) -> str:
    values = read_credentials(credentials, "postgres")
    return psycopg.conninfo.make_conninfo(**{k: str(v) for k, v in values.items()})


def _measure(args) -> None:
    import helpers

    admin = read_credentials(args.admin, "postgres")
    work = Path(tempfile.mkdtemp(prefix="event-buckets-"))
    walls: dict[str, list[float]] = {"trigger": [], "no trigger": []}
    for n in range(args.rounds):
        modes = ["trigger", "no trigger"] if n % 2 == 0 else ["no trigger", "trigger"]
        for mode in modes:
            database = helpers.scratch_database(admin)
            values = admin | {"dbname": database}
            direct = credentials_file(values, work / f"{database}.toml")
            product_id = prepare(direct, mode == "trigger")
            with Proxy(values["host"], values["port"], args.delay) as proxy:
                info = conninfo(proxied(values, proxy, work))
                began = time.monotonic()
                asyncio.run(emit_hour(info, product_id, args.cells))
                wall = time.monotonic() - began
            with storage.connect(direct) as conn:
                stored = conn.execute("SELECT count(*) FROM test_events").fetchone()[0]
                buckets = conn.execute("SELECT coalesce(sum(events),0) FROM test_event_buckets")
                counted = buckets.fetchone()[0]
            walls[mode].append(wall)
            record = {"round": n, "case": mode, "cells": args.cells, "events": stored}
            record |= {"bucket_events": counted, "wall_seconds": round(wall, 3)}
            record |= {"statements": proxy.statements(), "round_trips": proxy.round_trips()}
            print(json.dumps(record), flush=True)
            helpers.drop_databases(admin, [database])
    medians = {mode: statistics.median(w) for mode, w in walls.items()}
    share = medians["trigger"] / medians["no trigger"] - 1
    print(json.dumps({"median_seconds": medians, "trigger_share": round(share, 4)}), flush=True)
    helpers.drop_queued()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--admin", default=os.environ.get("PG_TEST_ADMIN_FILE"))
    parser.add_argument("--delay", type=float, default=0.039, help="seconds each way")
    parser.add_argument("--cells", type=int, default=72)
    parser.add_argument("--rounds", type=int, default=5)
    sys.exit(_measure(parser.parse_args()))
