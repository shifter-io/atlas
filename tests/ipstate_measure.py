"""CALC-SCALE-1's measurement of the IP state (calculated/ipstate.py) at a size the hourly build
reaches with weeks of data: --sightings rows in calculated_scope_ip_last_seen (one IP in ten seen
by two scopes) spread over 30 days, and one new hour of --observations observation rows (two
thirds new IPs) in 12 buckets. It times the first count of the vectors from the table (the first
build after the deploy, or a state counted again) and then one hourly advance(), which folds the
new hour and moves the vectors by the IPs that changed, and prints its plan with --explain.

The tables are plain copies of the columns ipstate reads, in a scratch schema, so no foreign key
or partition is needed. On a disposable server with PG_TEST_ADMIN_FILE set:
    python tests/ipstate_measure.py [--sightings 10000000 30000000] [--observations 400000]
prints one JSON line per size.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from atlas.calculated import ipstate  # noqa: E402
from atlas.providers.credentials import read_credentials  # noqa: E402

AS_OF = datetime(2026, 12, 1, tzinfo=UTC)
HOURS = [24, 168, 720]
TABLES = (
    "DROP SCHEMA IF EXISTS m CASCADE",
    "CREATE SCHEMA m",
    "SET search_path TO m",
    "CREATE TABLE test_runs(run_key bigint PRIMARY KEY,scope_id integer,batch_id bigint)",
    "CREATE TABLE ip_observations(run_key bigint,bucket_start timestamptz,ip_id bigint,"
    "PRIMARY KEY(run_key,bucket_start,ip_id))",
    "CREATE INDEX ON ip_observations(bucket_start,ip_id)",
    "CREATE TABLE calculated_bucket_ips(bucket_start timestamptz PRIMARY KEY,"
    "runs_digest text NOT NULL,unique_ips bigint NOT NULL)",
)
FILL = (
    "INSERT INTO test_runs SELECT g,g%%4000,CASE WHEN g%%3=0 THEN 1 END "
    "FROM generate_series(1,300000) g",
    # Sightings spread over the 30 days before the last hour; one IP in ten in two scopes.
    "INSERT INTO calculated_scope_ip_last_seen SELECT g,(g::bigint*7919)%%8000,"
    "%(as_of)s-interval '1 hour'-((g::bigint*2654435761)%%8640+1)*interval '5 minutes' "
    "FROM generate_series(1,%(n)s) g",
    "INSERT INTO calculated_scope_ip_last_seen SELECT g*10,(g::bigint*7919)%%8000+1,"
    "%(as_of)s-interval '1 hour'-((g::bigint*40503)%%8640+1)*interval '5 minutes' "
    "FROM generate_series(1,%(n)s/10) g ON CONFLICT DO NOTHING",
    "INSERT INTO calculated_bucket_ips SELECT t,'x',0,'x' FROM generate_series("
    "%(as_of)s-interval '720 hours',%(as_of)s-interval '65 minutes',interval '5 minutes') t",
    # The new hour: two thirds new IPs, one third seen before.
    "INSERT INTO ip_observations SELECT 1+(g::bigint*48271)%%300000,"
    "%(as_of)s-interval '1 hour'+(g%%12)*interval '5 minutes',"
    "CASE WHEN g%%3=0 THEN 1+(g::bigint*37)%%%(n)s ELSE %(n)s+g END "
    "FROM generate_series(1,%(rows)s) g ON CONFLICT DO NOTHING",
    "ANALYZE",
)


def measure(conn, sightings, rows, explain):
    for statement in TABLES:
        conn.execute(statement)
    migration = Path(ipstate.__file__).with_name("migrate_v15_v16.sql").read_text()
    conn.execute(migration)
    for statement in FILL:
        conn.execute(statement, {"as_of": AS_OF, "n": sightings, "rows": rows})
    conn.commit()
    cursor = conn.cursor()
    start = time.perf_counter()
    ipstate._move(cursor, AS_OF - timedelta(hours=1), HOURS, None, None)
    cursor.execute(
        "INSERT INTO calculated_ip_vector_state VALUES (%s,%s,'{}')",
        (AS_OF - timedelta(hours=1), HOURS),
    )
    conn.commit()
    counted = time.perf_counter() - start
    buckets = [AS_OF - timedelta(minutes=5 * n) for n in range(1, 8641)]
    runs = {t: "y" if t >= AS_OF - timedelta(hours=1) else "x" for t in buckets}
    if explain:
        print_plan(conn, runs)
    start = time.perf_counter()
    vectors = ipstate.advance(conn, AS_OF, HOURS, runs, {})
    conn.commit()
    advanced = time.perf_counter() - start
    return {
        "sightings": sightings,
        "observations": rows,
        "count_seconds": round(counted, 1),
        "advance_seconds": round(advanced, 1),
        "vectors": sum(len(v.rows) for v in vectors.values()),
    }


def print_plan(conn, runs):
    """The plan of one advance's statement, run in a transaction rolled back."""
    changed = sorted(t for t, d in runs.items() if d == "y")
    since = [AS_OF - timedelta(hours=h) for h in HOURS]
    prev = [AS_OF - timedelta(hours=h + 1) for h in HOURS]
    bounds = {"hours": HOURS, "prev": prev, "since": since, "changed": changed}
    statement = ipstate.ADVANCE.format(source=ipstate.ALL)
    plan = conn.execute("EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) " + statement, bounds).fetchall()
    print("\n".join(row[0] for row in plan), file=sys.stderr)
    conn.rollback()
    conn.execute("SET search_path TO m")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sightings", type=int, nargs="+", default=[10_000_000, 30_000_000])
    parser.add_argument("--observations", type=int, default=400_000)
    parser.add_argument("--explain", action="store_true")
    args = parser.parse_args()
    admin = read_credentials(os.environ["PG_TEST_ADMIN_FILE"], "postgres")
    with psycopg.connect(**admin, autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS ipstate_measure")
        conn.execute("CREATE DATABASE ipstate_measure")
    with psycopg.connect(**(admin | {"dbname": "ipstate_measure"})) as conn:
        for sightings in args.sightings:
            print(json.dumps(measure(conn, sightings, args.observations, args.explain)))


if __name__ == "__main__":
    main()
