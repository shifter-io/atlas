"""DB-EXPLAIN-1's measurement: the API's rare-value reads of the live tape and the per-event insert,
without and with schema v15's partial test_events indexes, on a synthetic year of events.

The year: --routine routine events (daily tests started and finished, hourly estimate releases,
each with a product, no country or target) over 12 monthly partitions, and --notable rare ones
(pulse_down, latency_rise, ip_overlap in turn), each with a product, a country and a target. The
reads are the Go API's SQL (store.EventsQuery, Inputs.loadEvents), each run --rounds times under
EXPLAIN (ANALYZE, BUFFERS); the insert is --inserts single-row emits (orchestrator.events.emit's
statement, every tenth a notable one with a country and target) in a transaction rolled back.
First without the indexes (v14), then migrate_v14_v15.sql builds them (timed), then again (v15,
whose kinds and tab reads repeat the notable clause).
On a disposable server with PG_TEST_ADMIN_FILE set:
    python tests/events_index_measure.py [--routine 1000000] [--notable 300] [--rounds 5]
prints one JSON line per step, the medians, and every read's plan with and without the indexes.
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

from ingest_cases import credentials_file  # noqa: E402

from atlas import storage  # noqa: E402
from atlas.partitions import partition  # noqa: E402
from atlas.providers.credentials import read_credentials  # noqa: E402

FIRST = datetime(2025, 12, 1, tzinfo=UTC)
YEAR = timedelta(days=365)
INDEXES = ("test_events_notable", "test_events_country", "test_events_target")
ROUTINE_KINDS = "ARRAY['daily_test_started','daily_test_finished','estimate_hour_released']"
NOTABLE_KINDS = "ARRAY['pulse_down','latency_rise','ip_overlap']"
COUNTRIES = ("DE", "US", "BR")
# The Go API's statements (api/internal/store: EventColumns, eventsOrder, routineSQL).
COLUMNS = (
    "SELECT e.event_id, e.at, e.kind, p.provider, p.name, e.country_code,\n"
    " coalesce(r.slug, e.data->>'region'), e.target_id, e.signal, e.severity, e.data\n"
    " FROM public.test_events e LEFT JOIN public.products p ON p.product_id = e.product_id\n"
    " LEFT JOIN public.runner_regions r ON r.region_id = e.region_id"
)
ORDER = " ORDER BY e.at DESC, e.event_id DESC LIMIT "
NOT_ROUTINE = (
    "NOT (left(e.kind,7)='census_' OR e.kind IN\n"
    " ('daily_test_started','daily_test_finished','estimate_hour_released','pulse_finished'))"
)
CATEGORY = """CASE WHEN left(e.kind,7)='census_'
 OR e.kind IN ('daily_test_started','daily_test_finished','estimate_hour_released') THEN 'census'
 WHEN left(e.kind,6)='pulse_' THEN 'pulse'
 WHEN e.kind IN ('new_hosting_asn','ip_overlap') THEN 'audit' ELSE 'incident' END"""
EMIT = (
    "INSERT INTO test_events(at,kind,batch_id,product_id,country_code,target_id,region_id,signal,"
    "severity,data) VALUES (%s,%s,NULL,%s,%s,%s,NULL,%s,'info','{}') RETURNING event_id"
)


def reads(cursor_at: str, cursor_id: int) -> dict[str, tuple[str, str]]:
    """Each read's SQL at v14 and at v15 (the kinds and tab reads add the notable clause)."""
    since = f"e.at <= '{cursor_at}' AND (e.at, e.event_id) < ('{cursor_at}', {cursor_id})"

    def same(where: str, limit: int) -> tuple[str, str]:
        sql = f"{COLUMNS} WHERE {where}{ORDER}{limit}"
        return sql, sql

    def noted(where: str) -> tuple[str, str]:
        return same(where, 100)[0], f"{COLUMNS} WHERE {where} AND {NOT_ROUTINE}{ORDER}100"

    return {
        "load.events_notable": same(f"{NOT_ROUTINE} AND {since}", 501),
        "events.query.default_notable": same(NOT_ROUTINE, 50),
        "events.query.kinds=pulse_down": noted("e.kind=ANY('{pulse_down}')"),
        "events.query.category=incident": noted(f"{CATEGORY}='incident'"),
        "events.query.category=audit": noted(f"{CATEGORY}='audit'"),
        "events.query.country=DE": same("e.country_code='DE'", 100),
        "events.query.country=FR (none)": same("e.country_code='FR'", 100),
        "events.query.target=example": same("e.target_id='example'", 100),
        "events.query.target=other (none)": same("e.target_id='other'", 100),
    }


def setup(database: Path, args) -> int:
    """The v15 schema without its three indexes (v14's test_events), the rows events name, and
    the year; the product id."""
    storage.initialize(database)
    with storage.connect(database) as conn:
        for name in INDEXES:
            conn.execute(f"DROP INDEX {name}")
        conn.execute("INSERT INTO runner_regions(region_id,slug,pattern) VALUES (1,'r1','r1')")
        for code in (*COUNTRIES, "FR"):
            conn.execute(
                "INSERT INTO countries VALUES (%s,%s,1,'00:00')", (code, f"country {code}")
            )
        conn.execute("INSERT INTO targets(target_id,url) VALUES ('example','https://a.test')")
        conn.execute("INSERT INTO targets(target_id,url) VALUES ('other','https://b.test')")
        conn.execute("INSERT INTO providers VALUES ('fixture')")
        product = conn.execute(
            "INSERT INTO products(provider,name) VALUES ('fixture','residential') "
            "RETURNING product_id"
        ).fetchone()[0]
        for month in range(13):
            partition(conn, "test_events", FIRST + timedelta(days=31 * month))
    began = time.monotonic()
    with storage.connect(database) as conn:
        step = YEAR / args.routine
        conn.execute(
            f"INSERT INTO test_events(at,kind,product_id) SELECT %s+n*%s,"
            f"({ROUTINE_KINDS})[n%%3+1],%s FROM generate_series(0,%s-1) n",
            (FIRST, step, product, args.routine),
        )
        conn.execute(
            f"INSERT INTO test_events(at,kind,product_id,country_code,target_id,signal) "
            f"SELECT %s+n*%s+interval '17 seconds',({NOTABLE_KINDS})[n%%3+1],%s,"
            "(%s::text[])[n%%3+1],'example',-1 FROM generate_series(0,%s-1) n",
            (FIRST, YEAR / args.notable, product, list(COUNTRIES), args.notable),
        )
    with storage.connect(database) as conn:
        conn.autocommit = True
        conn.execute("VACUUM ANALYZE test_events")
        rows = conn.execute("SELECT count(*) FROM test_events").fetchone()[0]
        size = conn.execute(
            "SELECT sum(pg_total_relation_size(inhrelid))::bigint FROM pg_inherits "
            "WHERE inhparent='test_events'::regclass"
        ).fetchone()[0]
    record = {"step": "year", "events": rows, "mb": round(size / 1024**2, 1)}
    print(json.dumps(record | {"seconds": round(time.monotonic() - began)}), flush=True)
    return product


def explained(conn, query: str, form: str = "JSON"):
    """query under EXPLAIN (ANALYZE, BUFFERS): (ms, rows, buffers) in JSON, else the text plan."""
    rows = conn.execute(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT {form}) {query}").fetchall()
    if form != "JSON":
        return "\n".join(row[0] for row in rows)
    plan = rows[0][0][0]
    hit, read = (plan["Plan"].get(k, 0) for k in ("Shared Hit Blocks", "Shared Read Blocks"))
    return plan["Planning Time"] + plan["Execution Time"], plan["Plan"]["Actual Rows"], hit + read


def inserts(conn, product: int, args) -> float:
    """Microseconds per emitted event, --inserts of them in a transaction rolled back."""
    at = FIRST + YEAR - timedelta(days=1)
    conn.execute("BEGIN")
    began = time.perf_counter()
    for n in range(args.inserts):
        rare = n % 10 == 0
        values = (at + timedelta(seconds=n), "pulse_down" if rare else "daily_test_finished")
        values += (product, "DE" if rare else None, "example" if rare else None, -1 if rare else 0)
        conn.execute(EMIT, values)
    spent = time.perf_counter() - began
    conn.execute("ROLLBACK")
    return spent / args.inserts * 1e6


def measure(conn, state: str, product: int, args) -> dict:
    """Every read's median ms and the insert's median µs in this state; prints the plans."""
    cursor = conn.execute(
        "SELECT at::text,event_id FROM test_events "
        "ORDER BY at DESC,event_id DESC OFFSET 1999 LIMIT 1"
    ).fetchone()
    medians = {}
    for name, sqls in reads(*cursor).items():
        sql = sqls[state == "v15"]
        runs = [explained(conn, sql) for _ in range(args.rounds)]
        ms = round(statistics.median(r[0] for r in runs), 2)
        record = {"state": state, "read": name, "median_ms": ms, "rows": runs[0][1]}
        print(json.dumps(record | {"buffers": runs[0][2]}), flush=True)
        print(f"--- {state} {name}\n{explained(conn, sql, 'TEXT')}", flush=True)
        medians[name] = ms
    per_event = statistics.median(inserts(conn, product, args) for _ in range(args.rounds))
    medians["insert_us_per_event"] = round(per_event, 1)
    print(json.dumps({"state": state, "insert_us_per_event": medians["insert_us_per_event"]}))
    return medians


def build(database: Path) -> None:
    """migrate_v14_v15.sql in one transaction, timed, then ANALYZE (as after a migration)."""
    sql = Path(storage.__file__).with_name("migrate_v14_v15.sql").read_text()
    began = time.monotonic()
    with storage.connect(database) as conn:
        conn.execute(sql)
    seconds = time.monotonic() - began
    with storage.connect(database) as conn:
        conn.autocommit = True
        conn.execute("ANALYZE test_events")
        size = conn.execute(
            "SELECT sum(pg_relation_size(i.inhrelid))::bigint FROM pg_inherits i "
            "WHERE i.inhparent::regclass::text = ANY(%s)",
            (list(INDEXES),),
        ).fetchone()[0]
    record = {"step": "migrate_v14_v15", "seconds": round(seconds, 2), "index_kb": size // 1024}
    print(json.dumps(record), flush=True)


def _measure(args) -> None:
    import helpers

    admin = read_credentials(args.admin, "postgres")
    work = Path(tempfile.mkdtemp(prefix="events-index-"))
    name = helpers.scratch_database(admin)
    database = credentials_file(admin | {"dbname": name}, work / f"{name}.toml")
    try:
        product = setup(database, args)
        with storage.connect(database) as conn:
            conn.autocommit = True
            before = measure(conn, "v14", product, args)
        build(database)
        with storage.connect(database) as conn:
            conn.autocommit = True
            after = measure(conn, "v15", product, args)
        print(json.dumps({"v14": before, "v15": after}), flush=True)
    finally:
        helpers.drop_databases(admin, [name])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--admin", default=os.environ.get("PG_TEST_ADMIN_FILE"))
    parser.add_argument("--routine", type=int, default=1_000_000)
    parser.add_argument("--notable", type=int, default=300)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--inserts", type=int, default=2000)
    sys.exit(_measure(parser.parse_args()))
