"""Two imports that both need a new day's partitions both commit (storage.ingest's lock)."""

import asyncio
import gzip
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta

from test_storage import measurement
from test_storage_v6 import legacy, save

from atlas.partitions import partition
from atlas.storage import connect, ingest

pytest_plugins = ["test_storage"]

DAY = date(2026, 12, 3)
WAITING = (
    "SELECT count(*) FROM pg_locks l LEFT JOIN pg_class c ON c.oid=l.relation WHERE NOT l.granted "
    "AND l.database=(SELECT oid FROM pg_database WHERE datname=current_database()) "
    "AND (l.locktype='advisory' OR c.relname='run_buckets')"
)


def across_midnight(directory):
    """End the run's buckets at 23:55 UTC on DAY and add an empty one at the next midnight."""
    summary = legacy(directory)
    end = datetime(DAY.year, DAY.month, DAY.day, 23, 55, tzinfo=UTC)
    shift = end - datetime.fromisoformat(summary["buckets"][-1]["bucket_start"])

    def moved(value):
        return (datetime.fromisoformat(value) + shift).isoformat(timespec="microseconds")

    for bucket in summary["buckets"]:
        bucket["bucket_start"] = moved(bucket["bucket_start"])
    empty = {k: 0 if type(v) is int else {} for k, v in summary["buckets"][-1].items()}
    empty["latency_histogram"] = {**summary["buckets"][-1]["latency_histogram"], "bins": []}
    midnight = (end + timedelta(minutes=5)).isoformat(timespec="microseconds")
    summary["buckets"].append(empty | {"bucket_start": midnight})
    source = directory / "ips.jsonl.gz"
    with gzip.open(source, "rt") as stream:
        rows = [json.loads(line) for line in stream]
    for row in rows:
        for key in ("bucket_start", "first_seen", "last_seen"):
            row[key] = moved(row[key])
    with gzip.open(source, "wt") as stream:
        stream.writelines(json.dumps(row) + "\n" for row in rows)
    summary["artifacts"]["ips"]["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    save(directory, summary)


async def test_imports_needing_the_same_new_day_both_commit(database, tmp_path):
    """Each import inserts DAY's buckets, then creates the next day's partitions. Without the
    ingest lock both would hold ROW EXCLUSIVE on run_buckets and wait for each other's ACCESS
    EXCLUSIVE (a deadlock); a holder of the parent makes both reach that point together. Other
    providers and targets, so no shared row (providers, targets) orders them first."""
    runs = [
        await measurement(tmp_path / "a", provider="fixture-a", target="generic_https"),
        await measurement(tmp_path / "b", provider="fixture-b", target="generic_http"),
    ]
    for directory in runs:
        across_midnight(directory)
    with connect(database) as conn:
        for table in ("run_buckets", "ip_observations"):
            partition(conn, table, datetime(DAY.year, DAY.month, DAY.day, tzinfo=UTC))
    holder = connect(database)
    holder.execute("LOCK TABLE ONLY run_buckets IN ACCESS SHARE MODE")
    with ThreadPoolExecutor(max_workers=2) as pool:
        imports = [pool.submit(ingest, directory, database) for directory in runs]
        try:
            for _ in range(1000):
                with connect(database) as conn:
                    if conn.execute(WAITING).fetchone()[0] == 2:
                        break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("the imports never both waited")
        finally:
            holder.close()  # releases the parent
        results = [future.result(timeout=60) for future in imports]
    assert all(result["imported"] for result in results)
    with connect(database) as conn:
        days = conn.execute(
            "SELECT DISTINCT run_key,(bucket_start AT TIME ZONE 'UTC')::date FROM run_buckets"
        ).fetchall()
    keys = [result["run_key"] for result in results]
    assert sorted(days) == sorted((k, d) for k in keys for d in (DAY, DAY + timedelta(days=1)))
