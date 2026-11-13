"""INGEST-PARALLEL-1's measurement: an hour's daily tests stored over N connections at once
(INGEST-PROCESSES-1: in P worker processes).

On a disposable server with PG_TEST_ADMIN_FILE set, after delaying the loopback link (77 ms
round trips: `tc qdisc add dev lo root netem delay 38.5ms limit 100000`):
    python tests/parallel_measure.py [--tests 200] [--batch 20] [--ips 2000] \
        [--connections 1 10 25 40] [--processes 1 2 4 7]
stores the same synthetic daily tests (ingest_cases.daily_test, 30% of each test's IPs shared
with the next) in batches of `--batch`, each batch one network's (`--networks` 18, as an hour's
batches are whole cells), on a fresh database for each N and P, the day partitions made
beforehand, as an hour's mostly are. One JSON line per N and P: wall seconds, rows stored a
second, MB a second over the loopback link (both directions), daily tests a minute, the
client's CPU (cores busy and seconds a test: this process's threads and its worker processes)
and the host's (all cores, the database's included), and the seconds connections spent
waiting for each lock (pg_locks, sampled every 50 ms); then whether every N and P
stored the same rows.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import sys
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ingest_cases import START, credentials_file, daily_test, dump  # noqa: E402

from atlas import partitions, storage, storage_parallel  # noqa: E402

# Each waiting lock's kind: the import's advisory locks by key, else the lock's type.
WAITS = f"""SELECT CASE WHEN locktype<>'advisory' THEN locktype
 WHEN objsubid=1 AND objid::bigint={storage.INGEST_LOCK} THEN 'ingest'
 WHEN objsubid=1 AND objid::bigint={storage.OVERLAP_LOCK} THEN 'overlap'
 WHEN objsubid=2 AND classid::bigint={storage.RUN_LOCK} THEN 'run'
 WHEN objsubid=2 AND classid::bigint={storage.NETWORK_LOCK} THEN 'network' ELSE 'advisory' END,
 count(*) FROM pg_locks WHERE NOT granted GROUP BY 1"""
INTERVAL = 0.05
# The import connections by state and wait (pg_stat_activity), this one left out.
ACTIVITY = """SELECT state||' '||coalesce(wait_event_type||':'||wait_event,'-'),count(*)
 FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() GROUP BY 1"""


class Waits(threading.Thread):
    """Seconds spent waiting per lock kind, sampled on a connection of its own, and the
    connections' states when the most were waiting on a lock."""

    def __init__(self, credentials: Path):
        super().__init__(daemon=True)
        self.credentials, self.seconds, self.stop = credentials, {}, threading.Event()
        self.busiest: dict = {}

    def run(self) -> None:
        with storage.connect(self.credentials) as conn:
            conn.autocommit = True
            while not self.stop.wait(INTERVAL):
                found = dict(conn.execute(WAITS).fetchall())
                for kind, count in found.items():
                    self.seconds[kind] = self.seconds.get(kind, 0) + count * INTERVAL
                if sum(found.values()) > self.busiest.get("waiting", -1):
                    self.busiest = dict(conn.execute(ACTIVITY).fetchall())
                    self.busiest["waiting"] = sum(found.values())

    def result(self) -> tuple[dict, dict]:
        self.stop.set()
        self.join()
        waits = {kind: round(value, 1) for kind, value in sorted(self.seconds.items())}
        return waits, self.busiest


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


def _client_cpu() -> float:
    """CPU seconds of this process and of its worker processes ended so far (each store's are
    joined before it returns)."""
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return time.process_time() + children.ru_utime + children.ru_stime


def _measure(args) -> None:
    import helpers

    from atlas.providers.credentials import read_credentials

    storage_parallel.RESERVE = storage_parallel.RESERVE_SHARE = 0  # the server is its own
    admin = read_credentials(args.admin, "postgres")
    work = Path(tempfile.mkdtemp(prefix="ingest-parallel-"))
    size = args.batch
    networks = [f"n{i // size % args.networks}" for i in range(args.tests)]
    tests = [
        daily_test(work / f"d{i}", i, ips=args.ips, repeats=args.ips // 2, provider=network)
        for i, network in enumerate(networks)
    ]
    groups = [
        storage_parallel.Group(tests[k : k + size], networks=frozenset(networks[k : k + size]))
        for k in range(0, len(tests), size)
    ]
    stored = {}
    for n, p in [(n, p) for n in args.connections for p in args.processes if p <= n]:
        database = helpers.scratch_database(admin)
        direct = credentials_file(admin | {"dbname": database}, work / f"{database}.toml")
        storage.initialize(direct)
        _partitions(direct, args.tests // 24 + 2)
        began, cpu, sent, host = time.monotonic(), _client_cpu(), _link_bytes(), _host_cpu()
        waits = Waits(direct)
        waits.start()
        outcomes = storage_parallel.store(groups, direct, connections=n, processes=p)
        wall = time.monotonic() - began
        record_waits, busiest = waits.result()
        cpu, sent = _client_cpu() - cpu, _link_bytes() - sent
        busy, total = (after - before for after, before in zip(_host_cpu(), host, strict=True))
        rows = sum(storage.status(direct)["counts"].values())
        with storage.connect(direct) as conn:
            text = json.dumps(dump(conn), sort_keys=True).encode()
            stored[n, p] = hashlib.sha256(text).hexdigest()
        record = {"connections": n, "processes": p, "tests": args.tests, "batch": args.batch}
        record["rows"] = rows
        record |= {"wall_seconds": round(wall, 2), "rows_per_second": round(rows / wall)}
        record |= {"mb_per_second": round(sent / wall / 1e6, 2)}
        record |= {"tests_per_minute": round(args.tests * 60 / wall, 1)}
        record |= {"client_cores": round(cpu / wall, 2), "host_busy": round(busy / total, 2)}
        record["client_cpu_per_test"] = round(cpu / args.tests, 3)
        record |= {"retries": sum(o.retries for o in outcomes)}
        record["failed"] = sum(o.error is not None for o in outcomes)
        record["lock_wait_seconds"] = record_waits
        record["busiest_moment"] = busiest
        print(json.dumps(record), flush=True)
        helpers.drop_databases(admin, [database])
    print(json.dumps({"same_rows": len(set(stored.values())) == 1}), flush=True)
    helpers.drop_queued()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--admin", default=os.environ.get("PG_TEST_ADMIN_FILE"))
    parser.add_argument("--tests", type=int, default=200)
    parser.add_argument("--batch", type=int, default=20)
    parser.add_argument("--ips", type=int, default=2000)
    parser.add_argument("--networks", type=int, default=18)
    parser.add_argument("--connections", type=int, nargs="+", default=[1, 10, 25, 40])
    parser.add_argument("--processes", type=int, nargs="+", default=[1])
    sys.exit(_measure(parser.parse_args()))
