"""CALC-SCALE-2's measurement of the hourly build's IP step against days of data: the `*` bucket
memos and IP last seen (fleetsql.bucket_ips()) and the IP state (ipstate.advance()) with
--per-day observation rows a day (50 runs a 5-minute bucket, one IP in three seen before) for
each of --days days before the last hour, and one new hour of the same rate.

For each size it times what each hourly build did before CALC-SCALE-2, when a late verdict (each
hour has them) emptied both memos: everything folded from the observations again ("reset"), and
what it does now that they are kept: the new hour folded and the vectors moved ("hourly"). With
--explain it prints the plans (EXPLAIN ANALYZE, BUFFERS) of the heaviest statement of each:
the reset's fold of every observation into the IP state, and the hourly ADVANCE.

The tables are plain copies of the columns these read, in a scratch schema. On a disposable
server with PG_TEST_ADMIN_FILE set:
    python tests/late_verdicts_measure.py [--days 3 10 30] [--per-day 1000000] [--explain]
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

from atlas.calculated import fleetsql, ipstate  # noqa: E402
from atlas.providers.credentials import read_credentials  # noqa: E402

AS_OF = datetime(2026, 12, 1, tzinfo=UTC)
HOURS = [24, 168, 720]
RUNS = 50  # runs a bucket, each in one bucket
BUCKET = timedelta(minutes=5)
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
    "CREATE TABLE calculated_ip_last_seen(ip_id bigint PRIMARY KEY,last_seen timestamptz NOT NULL)",
    "CREATE INDEX ON calculated_ip_last_seen(last_seen)",
)
# Bucket b (0: the newest before the last hour) of the days, RUNS runs each; IPs from a pool of a
# third of the rows, so one IP in three is seen again.
FILL = (
    "INSERT INTO test_runs SELECT g,g%%4000,CASE WHEN g%%3=0 THEN 1 END "
    "FROM generate_series(1,%(runs)s) g",
    "INSERT INTO ip_observations SELECT 1+(g%%%(buckets)s)*%(per)s+g%%%(per)s,"
    "%(prev)s-interval '5 minutes'-(g%%%(buckets)s)*interval '5 minutes',"
    "1+(g::bigint*2654435761)%%(%(rows)s/3) FROM generate_series(1,%(rows)s) g "
    "ON CONFLICT DO NOTHING",
    # The new hour: two thirds new IPs, one third seen before.
    "INSERT INTO ip_observations SELECT %(first)s+(g%%12)*%(per)s+g%%%(per)s,"
    "%(prev)s+(g%%12)*interval '5 minutes',CASE WHEN g%%3=0 THEN 1+(g::bigint*37)%%(%(rows)s/3) "
    "ELSE %(rows)s+g END FROM generate_series(1,%(hour)s) g ON CONFLICT DO NOTHING",
    "ANALYZE",
)


def measure(conn, days, per_day, explain):
    for statement in TABLES:
        conn.execute(statement)
    conn.execute(Path(ipstate.__file__).with_name("migrate_v15_v16.sql").read_text())
    buckets, prev = days * 288, AS_OF - timedelta(hours=1)
    sizes = {"buckets": buckets, "per": RUNS, "rows": per_day * days, "hour": per_day // 24}
    sizes |= {"runs": (buckets + 12) * RUNS, "first": buckets * RUNS + 1, "prev": prev}
    for statement in FILL:
        conn.execute(statement, sizes)
    conn.commit()
    old = {prev - (n + 1) * BUCKET: "x" for n in range(min(buckets, 8640))}
    new = {t: d for t, d in old.items() if t >= AS_OF - timedelta(hours=720)}
    new |= {prev + n * BUCKET: "y" for n in range(12)}
    if explain:
        plan(conn, ipstate.SAVE.format(rows=ipstate.FOLD.format(source=ipstate.ALL)), old)
    reset = fold(conn, prev, old)  # each hour before: both memos emptied, everything folded
    if explain:
        plan(conn, ipstate.ADVANCE.format(source=ipstate.ALL), new, prev)
    hourly = fold(conn, AS_OF, new)  # now: the new hour folded, the vectors moved
    return {
        "days": days,
        "observations": per_day * days,
        "reset_seconds": round(reset, 1),
        "hourly_seconds": round(hourly, 1),
    }


def fold(conn, as_of, runs):
    """The seconds of one latest build's IP step at as_of over the buckets' run digests."""
    start = time.perf_counter()
    fleetsql.bucket_ips(conn, as_of - timedelta(hours=HOURS[-1]), as_of, runs, fold=True)
    ipstate.advance(conn, as_of, HOURS, runs, {})
    conn.commit()
    return time.perf_counter() - start


def plan(conn, statement, runs, prev=None):
    """The plan of statement over the buckets of runs not folded yet, rolled back."""
    folded = {r[0] for r in conn.execute("SELECT bucket_start FROM calculated_bucket_ips")}
    as_of = max(runs) + BUCKET
    since = [as_of - timedelta(hours=h) for h in HOURS]
    bounds = {"changed": sorted(set(runs) - folded), "hours": HOURS, "since": since}
    bounds["prev"] = [(prev or as_of) - timedelta(hours=h) for h in HOURS]
    found = conn.execute("EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) " + statement, bounds).fetchall()
    print("\n".join(row[0] for row in found), file=sys.stderr)
    conn.rollback()
    conn.execute("SET search_path TO m")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, nargs="+", default=[3, 10, 30])
    parser.add_argument("--per-day", type=int, default=1_000_000)
    parser.add_argument("--explain", action="store_true")
    args = parser.parse_args()
    admin = read_credentials(os.environ["PG_TEST_ADMIN_FILE"], "postgres")
    with psycopg.connect(**admin, autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS late_verdicts_measure")
        conn.execute("CREATE DATABASE late_verdicts_measure")
    with psycopg.connect(**(admin | {"dbname": "late_verdicts_measure"})) as conn:
        for days in args.days:
            print(json.dumps(measure(conn, days, args.per_day, args.explain)), flush=True)


if __name__ == "__main__":
    main()
