"""SCOPE-IPS-1's measurement: one group's count of the IPs new to each scope, v13's scan of
ip_observations against v14's scope_ips, on a synthetic history of --gigabytes.

The history: --scopes scopes (rows made by importing one small ingest_cases daily test each),
each with --runs runs a day of --ips IPs over daily partitions, drawn from the scope's own pool
of 3 × --ips addresses, filled in SQL until ip_observations (table and indexes) holds
--gigabytes. scope_ips is then made again by migrate_v13_v14.sql (timed). The group:
one new run per scope, half of its IPs seen before in its scope, its observations stored and its
incoming_runs and incoming_observations built as ingest builds them, before _network. Each round
runs both statements under EXPLAIN (ANALYZE, BUFFERS), rolled back, in alternating order.
On a disposable server with PG_TEST_ADMIN_FILE set:
    python tests/scope_ips_measure.py [--gigabytes 5] [--scopes 20] [--ips 4300] [--rounds 5]
prints one JSON line per step and round, then the medians and both plans.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ingest_cases import credentials_file, daily_test  # noqa: E402
from test_scope_ips import V13_COUNT  # noqa: E402

from atlas import storage, storage_ingest  # noqa: E402
from atlas.partitions import partition  # noqa: E402
from atlas.providers.credentials import read_credentials  # noqa: E402

FIRST = datetime(2026, 8, 1, tzinfo=UTC)
SIZE = (
    "SELECT coalesce(sum(pg_total_relation_size(inhrelid)),0)::bigint FROM pg_inherits "
    "WHERE inhparent='ip_observations'::regclass"
)
# A day's runs per scope copy the first stored run's columns; their run keys, by scope.
RUNS = (
    "INSERT INTO test_runs(run_id,scope_id,started_at,finished_at,status,summary_sha256,summary) "
    "SELECT gen_random_uuid(),s.scope_id,%(day)s+r*interval '1 minute',"
    "%(day)s+r*interval '1 minute',t.status,t.summary_sha256,t.summary "
    "FROM (SELECT * FROM test_runs ORDER BY run_key LIMIT 1) t,scopes s,generate_series(1,%(n)s) r "
    "RETURNING run_key,scope_id"
)
# Each run's IPs: --ips consecutive addresses of its scope's pool from a per-run offset.
OBSERVATIONS = (
    "INSERT INTO ip_observations SELECT %(day)s+(i%%12)*interval '5 minutes',d.run_key,"
    "p.base+(d.run_key*7919+i)%%p.size,1,%(day)s+(i%%12)*interval '5 minutes',"
    "%(day)s+(i%%12)*interval '5 minutes' FROM day_runs d JOIN pools p USING(scope_id),"
    "generate_series(0,%(ips)s-1) i"
)
POOL_START = "(SELECT min(ip_id) FROM ip_addresses WHERE ip>='10.1.0.0')"
NEW_START = "(SELECT min(ip_id) FROM ip_addresses WHERE ip>='10.64.0.0')"


def captured(build) -> str:
    """The statement a storage_ingest function executes."""

    class Capture:
        def execute(self, query):
            self.query = query

    capture = Capture()
    build(capture)
    return capture.query


def setup(database: Path, work: Path, args) -> None:
    """The scopes (one small imported run each), their pools and the scopes table."""
    storage.initialize(database)
    runs = [
        daily_test(work / f"s{k}", k, ips=10, repeats=0, provider=f"net-{k}")
        for k in range(args.scopes)
    ]
    storage.ingest_many(runs, database)
    size = 3 * args.ips
    with storage.connect(database) as conn:
        conn.execute("CREATE TABLE scopes AS SELECT scope_id FROM scope_stats")
        conn.execute(
            "INSERT INTO ip_addresses(ip) SELECT '10.1.0.0'::inet+n "
            "FROM generate_series(0,%s-1) n ORDER BY n",
            (args.scopes * size,),
        )
        conn.execute(  # CREATE TABLE AS takes no parameters
            f"CREATE TABLE pools AS SELECT scope_id,{POOL_START}+"
            f"(row_number() OVER (ORDER BY scope_id)-1)*{size} AS base,{size} AS size FROM scopes"
        )
        gaps = conn.execute(
            "SELECT count(*) FROM ip_addresses WHERE ip>='10.1.0.0' AND ip<'10.64.0.0' "
            f"AND ip_id-{POOL_START}<>ip-'10.1.0.0'"
        ).fetchone()[0]
        assert gaps == 0, "pool addresses must have consecutive ids"


def history(database: Path, args) -> datetime:
    """Fill a day at a time until ip_observations holds --gigabytes; the first day after it."""
    day, target = FIRST, args.gigabytes * 1024**3
    began = time.monotonic()
    while True:
        with storage.connect(database) as conn:
            size = conn.execute(SIZE).fetchone()[0]
            if size >= target:
                rows = conn.execute("SELECT count(*) FROM ip_observations").fetchone()[0]
                break
            partition(conn, "ip_observations", day)
            conn.execute(
                "CREATE TEMP TABLE day_runs (run_key bigint,scope_id bigint) ON COMMIT DROP"
            )
            conn.execute(
                f"WITH r AS ({RUNS}) INSERT INTO day_runs SELECT * FROM r",
                {"day": day, "n": args.runs},
            )
            conn.execute(OBSERVATIONS, {"day": day, "ips": args.ips})
        day += timedelta(days=1)
    record = {"step": "history", "days": (day - FIRST).days, "observation_rows": rows}
    record |= {"gigabytes": round(size / 1024**3, 2), "seconds": round(time.monotonic() - began)}
    print(json.dumps(record), flush=True)
    return day


def backfill(database: Path) -> None:
    """migrate_v13_v14.sql in one transaction, timed (the table dropped first, as at v13), then
    VACUUM ANALYZE (as after a migration)."""
    sql = Path(storage.__file__).with_name("migrate_v13_v14.sql").read_text()
    with storage.connect(database) as conn:
        conn.execute("DROP TABLE scope_ips")
    began = time.monotonic()
    with storage.connect(database) as conn:
        conn.execute(sql)
    seconds = time.monotonic() - began
    with storage.connect(database) as conn:
        conn.autocommit = True
        conn.execute("VACUUM ANALYZE")
        rows = conn.execute("SELECT count(*) FROM scope_ips").fetchone()[0]
        sizes = conn.execute(
            "SELECT pg_table_size('scope_ips'),pg_indexes_size('scope_ips')"
        ).fetchone()
    record = {"step": "backfill", "scope_ips": rows, "seconds": round(seconds, 1)}
    record |= {"table_gb": round(sizes[0] / 1024**3, 2), "index_gb": round(sizes[1] / 1024**3, 2)}
    print(json.dumps(record), flush=True)


def group(conn, day: datetime, args) -> None:
    """The group's runs and observations stored, and ingest's temp tables of them (kept for the
    session): half of each run's IPs from its scope's pool (seen), half new addresses."""
    partition(conn, "ip_observations", day)
    conn.execute(
        "INSERT INTO ip_addresses(ip) SELECT '10.64.0.0'::inet+n FROM generate_series(0,%s-1) n",
        (args.scopes * args.ips,),
    )
    conn.execute("CREATE TEMP TABLE incoming_runs (idx bigint,run_key bigint,scope_id bigint)")
    conn.execute(
        f"WITH r AS ({RUNS}) INSERT INTO incoming_runs SELECT "
        "row_number() OVER (ORDER BY run_key)-1,run_key,scope_id FROM r",
        {"day": day, "n": 1},
    )
    conn.execute(
        "CREATE TEMP TABLE incoming_observations (idx bigint,bucket timestamptz,ip_id bigint,"
        "count bigint,run_key bigint,scope_id bigint)"
    )
    conn.execute(
        "INSERT INTO incoming_observations SELECT r.idx,%(day)s+(i%%12)*interval "
        f"'5 minutes',CASE WHEN i<%(half)s THEN p.base+i ELSE {NEW_START}"
        "+r.idx*%(ips)s+i END,1,r.run_key,r.scope_id "
        "FROM incoming_runs r JOIN pools p USING(scope_id),generate_series(0,%(ips)s-1) i",
        {"day": day, "half": args.ips // 2, "ips": args.ips},
    )
    conn.execute(
        "INSERT INTO ip_observations SELECT bucket,run_key,ip_id,count,bucket,bucket "
        "FROM incoming_observations"
    )
    for table in ("incoming_runs", "incoming_observations", "ip_observations"):
        conn.execute(f"ANALYZE {table}")


def explained(conn, query: str, form: str = "JSON"):
    """query under EXPLAIN (ANALYZE, BUFFERS) in a transaction rolled back: (execution ms,
    buffers) in JSON, else the text plan."""
    conn.execute("BEGIN")
    rows = conn.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT {form}) {query}").fetchall()
    conn.execute("ROLLBACK")
    if form != "JSON":
        return "\n".join(row[0] for row in rows)
    plan = rows[0][0][0]
    buffers = {k: plan["Plan"].get(k, 0) for k in ("Shared Hit Blocks", "Shared Read Blocks")}
    return plan["Execution Time"], buffers


def measure(database: Path, day: datetime, args) -> None:
    new = captured(storage_ingest.add_scope_ips)
    times: dict[str, list[float]] = {"v13": [], "v14": []}
    with storage.connect(database) as conn:
        conn.autocommit = True
        group(conn, day, args)
        old_counts = sorted(conn.execute(V13_COUNT).fetchall())
        conn.execute("BEGIN")
        new_counts = sorted(conn.execute(new).fetchall())
        conn.execute("ROLLBACK")
        assert old_counts == new_counts, (old_counts, new_counts)
        added = sum(n for _, n in new_counts)
        print(json.dumps({"step": "counts", "equal": True, "new_ips": added}), flush=True)
        for n in range(args.rounds):
            for case in ("v13", "v14") if n % 2 == 0 else ("v14", "v13"):
                ms, buffers = explained(conn, V13_COUNT if case == "v13" else new)
                times[case].append(ms)
                record = {"round": n, "case": case, "ms": round(ms, 1)}
                print(json.dumps(record | buffers), flush=True)
        medians = {case: round(statistics.median(t), 1) for case, t in times.items()}
        print(json.dumps({"median_ms": medians, "pairs": args.scopes * args.ips}), flush=True)
        for case, query in (("v13", V13_COUNT), ("v14", new)):
            print(f"--- {case} plan\n{explained(conn, query, 'TEXT')}", flush=True)


def _measure(args) -> None:
    import helpers

    admin = read_credentials(args.admin, "postgres")
    work = Path(tempfile.mkdtemp(prefix="scope-ips-"))
    name = helpers.scratch_database(admin)
    database = credentials_file(admin | {"dbname": name}, work / f"{name}.toml")
    try:
        setup(database, work, args)
        day = history(database, args)
        backfill(database)
        measure(database, day, args)
    finally:
        helpers.drop_databases(admin, [name])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--admin", default=os.environ.get("PG_TEST_ADMIN_FILE"))
    parser.add_argument("--gigabytes", type=float, default=5)
    parser.add_argument("--scopes", type=int, default=20)
    parser.add_argument("--ips", type=int, default=4300)
    parser.add_argument("--runs", type=int, default=12, help="runs a day per scope")
    parser.add_argument("--rounds", type=int, default=5)
    sys.exit(_measure(parser.parse_args()))
