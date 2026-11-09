"""INGEST-PARALLEL-1's measurement: an hour's daily tests stored over N connections at once.

On a disposable server with PG_TEST_ADMIN_FILE set, after delaying the loopback link (77 ms
round trips: `tc qdisc add dev lo root netem delay 38.5ms limit 100000`):
    python tests/parallel_measure.py [--tests 100] [--batch 4] [--ips 2000] \
        [--connections 1 10 25 40]
stores the same synthetic daily tests (ingest_cases.daily_test, 10 networks, 30% of each test's
IPs shared with the next) in batches of `--batch` on a fresh database for each N, the day
partitions made beforehand, as an hour's mostly are. One JSON line per N: wall seconds, rows
stored a second, MB a second over the loopback link (both directions), daily tests a minute, the
client's CPU (cores busy: this process's threads) and the host's (all cores, the database's
included); then whether every N stored the same rows.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ingest_cases import START, credentials_file, daily_test, dump  # noqa: E402

from proxy_index import partitions, storage, storage_parallel  # noqa: E402


def _partitions(credentials: Path, days: int) -> None:
    with storage.connect(credentials) as conn:
        for day in range(days):
            for table in partitions.DAILY:
                _, start, end = partitions.holder(table, START + timedelta(days=day), ())
                name = f"{table}_{start:%Y_%m_%d}"
                for statement in partitions.creation(table, name, start, end):
                    conn.execute(statement)


def _link_bytes() -> int:
    """Bytes sent over the loopback link so far (Linux /proc/net/dev)."""
    for line in Path("/proc/net/dev").read_text().splitlines():
        name, _, fields = line.partition(":")
        if name.strip() == "lo":
            return int(fields.split()[8])
    return 0


def _host_cpu() -> tuple[int, int]:
    """(busy, total) clock ticks of every core so far (Linux /proc/stat)."""
    ticks = [int(v) for v in Path("/proc/stat").read_text().split("\n")[0].split()[1:]]
    return sum(ticks) - ticks[3] - ticks[4], sum(ticks)


def _measure(args) -> None:
    import helpers

    from proxy_index.providers.credentials import read_credentials

    storage_parallel.RESERVE = 0  # the server is the measurement's own
    admin = read_credentials(args.admin, "postgres")
    work = Path(tempfile.mkdtemp(prefix="ingest-parallel-"))
    tests = [
        daily_test(work / f"d{i}", i, ips=args.ips, repeats=args.ips // 2, provider=f"n{i % 10}")
        for i in range(args.tests)
    ]
    networks, size = [f"n{i % 10}" for i in range(args.tests)], args.batch
    groups = [
        storage_parallel.Group(tests[k : k + size], networks=frozenset(networks[k : k + size]))
        for k in range(0, len(tests), size)
    ]
    stored = {}
    for n in args.connections:
        database = helpers.scratch_database(admin)
        direct = credentials_file(admin | {"dbname": database}, work / f"{database}.toml")
        storage.initialize(direct)
        _partitions(direct, args.tests // 24 + 2)
        began, cpu, sent, host = time.monotonic(), time.process_time(), _link_bytes(), _host_cpu()
        outcomes = storage_parallel.store(groups, direct, connections=n)
        wall = time.monotonic() - began
        cpu, sent = time.process_time() - cpu, _link_bytes() - sent
        busy, total = (after - before for after, before in zip(_host_cpu(), host, strict=True))
        rows = sum(storage.status(direct)["counts"].values())
        with storage.connect(direct) as conn:
            text = json.dumps(dump(conn), sort_keys=True).encode()
            stored[n] = hashlib.sha256(text).hexdigest()
        record = {"connections": n, "tests": args.tests, "batch": args.batch, "rows": rows}
        record |= {"wall_seconds": round(wall, 2), "rows_per_second": round(rows / wall)}
        record |= {"mb_per_second": round(sent / wall / 1e6, 2)}
        record |= {"tests_per_minute": round(args.tests * 60 / wall, 1)}
        record |= {"client_cores": round(cpu / wall, 2), "host_busy": round(busy / total, 2)}
        record |= {"retries": sum(o.retries for o in outcomes)}
        record["failed"] = sum(o.error is not None for o in outcomes)
        print(json.dumps(record), flush=True)
        helpers.drop_databases(admin, [database])
    print(json.dumps({"same_rows": len(set(stored.values())) == 1}), flush=True)
    helpers.drop_queued()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--admin", default=os.environ.get("PG_TEST_ADMIN_FILE"))
    parser.add_argument("--tests", type=int, default=100)
    parser.add_argument("--batch", type=int, default=4)
    parser.add_argument("--ips", type=int, default=2000)
    parser.add_argument("--connections", type=int, nargs="+", default=[1, 10, 25, 40])
    sys.exit(_measure(parser.parse_args()))
