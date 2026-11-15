#!/usr/bin/env python3
"""WIPE-TOOL-1: remove exactly the replay's data (REPLAY-1) from the database it ran in.

Usage:
  wipe_replay.py --database <toml> --state <replay state.json>
  wipe_replay.py --database <toml> --state <replay state.json> --apply \
                 --owner-go "owner-go LAUNCH-WIPE <YYYY-MM-DD>"

The dry run (the default) deletes in one transaction and rolls it back: it prints, per table,
the rows it would delete, and keeps them in wipe-dry-run.json beside the state. --apply runs
the same transaction, commits it only when every table's count equals the dry run's, and prints
the counts afterwards. REPLAY.md ("Wiping the replay") lists what is removed and what never is.
In production's database (PROD-AS-STAGING-1) the dry run and --apply need target.py's three
instead of the LAUNCH-WIPE go: --owner-go "owner-go PRODUCTION-AS-STAGING <YYYY-MM-DD>",
--i-know-this-is-production and ATLAS_ALLOW_PRODUCTION_TARGET=1.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import replay_baseline
import target
from browser_model import HOST as BROWSER_HOST
from psycopg import IsolationLevel, sql
from replay import write_private
from replay_steps import NODE_HOST

from atlas.calculated import netlists, store_retention
from atlas.partitions import DAILY, children
from atlas.storage import connect

LOCKS = (817452901, 817452902)  # storage.ingest's and the calculation's advisory locks
GO = re.compile(r"owner-go LAUNCH-WIPE (\d{4}-\d{2}-\d{2})")
GO_FORM = "'owner-go LAUNCH-WIPE <YYYY-MM-DD>'"
DRY_RUN = "wipe-dry-run.json"
SETS = (
    "CREATE TEMP TABLE wipe_runs ON COMMIT DROP AS SELECT run_key FROM test_runs "
    f"WHERE summary ? '{target.REPLAY_MARKER}' AND run_key>%(run)s",
    "CREATE TEMP TABLE wipe_nodes ON COMMIT DROP AS SELECT node_id FROM nodes "
    "WHERE node_id>%(node)s AND (host LIKE %(prefix)s OR host=%(browser)s OR node_id=ANY(%(ids)s))",
    # Batches whose runs all belong to the replay's nodes.
    "CREATE TEMP TABLE wipe_batches ON COMMIT DROP AS SELECT batch_id FROM test_batches b "
    "WHERE batch_id>%(batch)s AND EXISTS (SELECT FROM batch_runs r WHERE r.batch_id=b.batch_id) "
    "AND NOT EXISTS (SELECT FROM batch_runs r WHERE r.batch_id=b.batch_id "
    "AND (r.node_id IS NULL OR r.node_id NOT IN (SELECT node_id FROM wipe_nodes)))",
)
RUNS = "run_key IN (SELECT run_key FROM wipe_runs)"
BATCHES = "batch_id IN (SELECT batch_id FROM wipe_batches)"
HOURS = "{0}>=%(first)s AND {0}<%(end)s"
HOUR = HOURS.format("hour")
# A rollup hour the replay's calculations wrote: through_run is the newest run key when written.
ROLLED = "hour IN (SELECT hour FROM {} WHERE through_run>%(run)s)"
# (table, condition) in foreign-key order; a table the database lacks is skipped. The memos and
# rollups are caches the calculation rewrites when they are missing.
FACTS = (
    ("test_events", f"event_id>%(event)s AND ({BATCHES} OR at>=%(first)s AND at<=%(end)s)"),
    ("outages", f"outage_id>%(outage)s AND {BATCHES}"),
    *((name, RUNS) for name in ("run_buckets", "run_geo_buckets", "ip_observations")),
    *((name, RUNS) for name in ("run_enrichments", "estimated_hours", "calculation_roles")),
    ("country_verdicts", RUNS),  # the replay's daily tests' (schema v16)
    ("calculated_run_ranges", RUNS),
    ("batch_runs", f"{RUNS} OR {BATCHES} OR node_id IN (SELECT node_id FROM wipe_nodes)"),
    ("test_runs", RUNS),
    ("test_batches", BATCHES),
    ("nodes", "node_id IN (SELECT node_id FROM wipe_nodes)"),
    ("calculated_geo_hours", f"{HOUR} OR {ROLLED.format('calculated_geo_rollups')}"),
    ("calculated_pulse_buckets", f"{HOUR} OR {ROLLED.format('calculated_pulse_rollups')}"),
    ("calculated_geo_rollups", f"{HOUR} OR through_run>%(run)s"),
    ("calculated_pulse_rollups", f"{HOUR} OR through_run>%(run)s"),
    ("calculated_bucket_ips", HOURS.format("bucket_start")),
    ("calculated_ip_last_seen", HOURS.format("last_seen")),
    *((name, "last_used>=%(at)s") for name in ("calculated_distinct", "calculated_slot_figures")),
    ("load_references", "day>=%(first_day)s AND day<%(end_day)s AND chosen_at>=%(at)s"),
)
# Lookup tables in foreign-key order: a row new since the baseline (not among its keys, or above
# its highest identity) goes when no remaining row but a cache references it; the caches' rows
# that reference it go first. A table the baseline has neither for is left alone.
LOOKUPS = ("scope_stats", "pool_stats", "products", "providers", "ip_addresses")
LOOKUPS += ("dbip_releases", "enrichment_locations", "enrichment_networks", "asn_labels")
LOOKUPS += ("place_names", "asn_names", "cohorts", "score_reasons")
CACHES = ("product_ips", "pool_ips", "scope_ips", "provider_overlap", "outages")
# Caches of one lookup only: an IP's enrichment goes with the IP, but references its release,
# location and network while the IP stays. calculated_ip_last_seen has no foreign key.
OWN_CACHES = {"ip_addresses": [("ip_enrichment", "ip_id"), ("calculated_ip_last_seen", "ip_id")]}
REFERENCES = (
    "SELECT c.conrelid::regclass::text,a.attname FROM pg_constraint c JOIN pg_attribute a "
    "ON a.attrelid=c.conrelid AND a.attnum=c.conkey[1] WHERE c.contype='f' AND c.conparentid=0 "
    "AND cardinality(c.conkey)=1 AND c.confrelid=%s::regclass ORDER BY 1,2"
)
# The provider pairs the ingest updated since the baseline, counted again from the IPs both
# providers still have (the ingest only adds to a pair; nothing else rebuilds it).
OVERLAP = (
    "CREATE TEMP TABLE wipe_overlap ON COMMIT DROP AS SELECT o.provider_a,o.provider_b,"
    "(SELECT count(DISTINCT a.ip_id) FROM product_ips a JOIN products pa USING(product_id) "
    "JOIN product_ips b ON b.ip_id=a.ip_id JOIN products pb ON pb.product_id=b.product_id "
    "WHERE pa.provider=o.provider_a AND pb.provider=o.provider_b) AS shared "
    "FROM provider_overlap o WHERE o.updated_at>=%(at)s",
    "DELETE FROM provider_overlap o USING wipe_overlap w WHERE w.shared=0 "
    "AND (o.provider_a,o.provider_b)=(w.provider_a,w.provider_b)",
    "UPDATE provider_overlap o SET shared_ips=w.shared FROM wipe_overlap w WHERE w.shared>0 "
    "AND (o.provider_a,o.provider_b)=(w.provider_a,w.provider_b) AND o.shared_ips<>w.shared",
)
# The live tape's bucket counts of the replay's events (schema v13): the buckets they fall in,
# taken before the facts go, then recounted from the events left (the trigger counts inserts only).
BUCKET = "date_bin('30 minutes',at,'2000-01-01 00:00+00')"
EVENT_BUCKETS = (
    "CREATE TEMP TABLE wipe_event_buckets ON COMMIT DROP AS SELECT DISTINCT "
    f"{BUCKET} AS bucket_start FROM test_events WHERE {FACTS[0][1]}",
)
RECOUNT = (
    "DELETE FROM test_event_buckets WHERE bucket_start IN (SELECT * FROM wipe_event_buckets)",
    "INSERT INTO test_event_buckets SELECT * FROM test_event_bucket_counts "
    "WHERE bucket_start IN (SELECT * FROM wipe_event_buckets)",
)
CURRENT = (
    "SELECT max(b.build_id) FROM calculation_builds b JOIN calculated_snapshots USING(build_id) "
    "WHERE b.published AND b.build_id<=%s"
)


class Refused(Exception):
    """The go, the state or the dry run does not allow the wipe; nothing was deleted."""


def exists(conn, name: str) -> bool:
    return conn.execute("SELECT to_regclass(%s)", (name,)).fetchone()[0] is not None


def parameters(state: dict) -> dict:
    """The bounds of the replay's rows: the baseline's highest keys and the simulated days."""
    base = state["baseline"]
    top = {k.rsplit(".", 2)[1]: v or 0 for k, v in base["identities"].items()}
    first = datetime.combine(date.fromisoformat(state["first_day"]), datetime.min.time(), UTC)
    end = first + timedelta(days=state["days"])
    found = {"first": first, "end": end, "first_day": first.date(), "end_day": end.date()}
    found |= {"at": datetime.fromisoformat(base["at"])}
    found |= {"prefix": f"{NODE_HOST}%", "browser": BROWSER_HOST, "ids": state["nodes"]}
    names = {"run": "test_runs", "node": "nodes", "batch": "test_batches", "outage": "outages"}
    names |= {"event": "test_events"}
    return found | {key: top.get(name, 0) for key, name in names.items()}


def builds(conn, top: int) -> int | None:
    """Discard every build above the baseline; when one of them is current, the newest
    published baseline build left becomes current, with its network lists (none: no current
    build)."""
    row = conn.execute("SELECT build_id FROM current_calculation").fetchone()
    current = row[0] if row else None
    if current is not None and current > top:
        current = conn.execute(CURRENT, (top,)).fetchone()[0]
        if current is None:
            conn.execute("DELETE FROM current_calculation")
        else:
            conn.execute("UPDATE current_calculation SET build_id=%s", (current,))
    found = conn.execute("SELECT build_id FROM calculation_builds WHERE build_id>%s", (top,))
    for (build_id,) in found.fetchall():
        store_retention.discard(conn, build_id, gc=False)
    if store_retention.lists(conn):
        store_retention.collect(conn)
        netlists.restore(conn)  # a build made current again gets its network lists (publish())
    return current


def lookups(conn, base: dict) -> None:
    for name in (name for name in LOOKUPS if exists(conn, name)):
        [key] = [row[0] for row in conn.execute(replay_baseline.PRIMARY, (name,)).fetchall()]
        own = OWN_CACHES.get(name, [])
        refs = conn.execute(REFERENCES, (name,)).fetchall()
        refs = [ref for ref in sorted({*map(tuple, refs), *own}) if exists(conn, ref[0])]
        caches = {*CACHES, *(table for table, _ in own)}
        if name in base["keys"]:
            new = sql.SQL("l.{}::text<>ALL(%s::text[])").format(sql.Identifier(key))
            bound = [str(row[0]) for row in base["keys"][name]]
        elif f"public.{name}.{key}" in base["identities"]:
            new = sql.SQL("l.{}>%s").format(sql.Identifier(key))
            bound = base["identities"][f"public.{name}.{key}"] or 0
        else:
            continue
        kept = [
            sql.SQL("NOT EXISTS (SELECT FROM {} r WHERE r.{}=l.{})").format(
                sql.SQL(table), sql.Identifier(column), sql.Identifier(key)
            )
            for table, column in refs
            if table not in caches
        ]
        doomed = sql.SQL("CREATE TEMP TABLE wipe_doomed AS SELECT l.{} AS key FROM {} l WHERE {}")
        where = sql.SQL(" AND ").join([new, *kept])
        conn.execute(doomed.format(sql.Identifier(key), sql.Identifier(name), where), (bound,))
        for table, column in refs:
            if table in caches:
                gone = sql.SQL("DELETE FROM {} WHERE {} IN (SELECT key FROM wipe_doomed)")
                conn.execute(gone.format(sql.SQL(table), sql.Identifier(column)))
        gone = sql.SQL("DELETE FROM {} WHERE {} IN (SELECT key FROM wipe_doomed)")
        conn.execute(gone.format(sql.Identifier(name), sql.Identifier(key)))
        conn.execute("DROP TABLE wipe_doomed")


def partitions(conn, first: date, end: date, drop: bool) -> list[str]:
    """The empty daily partitions of the simulated days (dropped when `drop`)."""
    found = []
    for table in DAILY:
        for name, start, after in children(conn, table):
            if after - start != timedelta(days=1) or not first <= start < end:
                continue
            ident = sql.Identifier(name)
            if conn.execute(sql.SQL("SELECT 1 FROM {} LIMIT 1").format(ident)).fetchone():
                continue
            if drop:
                conn.execute(sql.SQL("DROP TABLE {}").format(ident))
            found.append(name)
    return found


def wipe(conn, state: dict, apply: bool) -> dict:
    """Delete the replay's rows in the open transaction; the deleted rows per table."""
    conn.execute("SET LOCAL lock_timeout='60s'")
    conn.execute("SET LOCAL statement_timeout=0")
    for lock in LOCKS:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock,))
    base, bounds = state["baseline"], parameters(state)
    before = replay_baseline.counts(conn)
    for statement in SETS:
        conn.execute(statement, bounds)
    top = base["identities"].get("public.calculation_builds.build_id") or 0
    current = builds(conn, top) if exists(conn, "calculation_builds") else None
    counted = exists(conn, "test_event_buckets")
    for statement in EVENT_BUCKETS if counted else ():
        conn.execute(statement, bounds)
    for name, condition in FACTS:
        if exists(conn, name):
            delete = sql.SQL(f"DELETE FROM {{}} WHERE {condition}")
            conn.execute(delete.format(sql.Identifier(name)), bounds)
    for statement in RECOUNT if counted else ():
        conn.execute(statement)
    lookups(conn, base)
    for statement in OVERLAP if exists(conn, "provider_overlap") else ():
        conn.execute(statement, bounds)
    dropped = partitions(conn, bounds["first"].date(), bounds["end"].date(), apply)
    after = replay_baseline.counts(conn)
    deleted = {name: before[name] - after[name] for name in before if before[name] != after[name]}
    known = {row[0] for row in base["keys"].get("targets", [])}
    added = [row[0] for row in conn.execute("SELECT target_id FROM targets ORDER BY 1")]
    differ = {
        name: {"baseline": base["counts"].get(name, 0), "after": count}
        for name, count in after.items()
        if count != base["counts"].get(name, 0)
    }
    return {"replay_id": state["replay_id"], "deleted": deleted, "current_build": current} | {
        "empty_partitions": dropped,
        "targets_kept": [name for name in added if name not in known],
        "counts": after,
        "not_at_baseline": differ,
    }


def refusal(conn, args, state: dict) -> tuple[str | None, dict | None]:
    """(refusal, None), or (None, production's go record when the database is production's)."""
    database = conn.execute("SELECT current_database()").fetchone()[0]
    if state["database"] != database:
        return f"the state is the replay of {state['database']!r}, not of {database!r}", None
    if database == target.PRODUCTION:  # the dry run too: it locks production's tables
        acknowledged = args.i_know_this_is_production
        return target.owner_go_refusal(conn, args.owner_go, acknowledged, runs=False)
    if not args.apply:
        return None, None
    if not args.owner_go:
        return f"--apply needs --owner-go {GO_FORM}", None
    return target.go_date_refusal(args.owner_go, datetime.now(UTC).date(), GO, GO_FORM), None


def run(args) -> dict:
    path = Path(args.state)
    state, dry = json.loads(path.read_text()), path.with_name(DRY_RUN)
    with connect(args.database) as conn:
        # One snapshot: the counts see the wipe's deletes, not other sessions' commits.
        conn.isolation_level = IsolationLevel.REPEATABLE_READ
        found, production = refusal(conn, args, state)
        if found:
            raise Refused(found)
        if args.apply and not dry.exists():
            raise Refused(f"no dry run ({dry}): run without --apply first")
        result = wipe(conn, state, args.apply) | {"production": production}
        if not args.apply:
            conn.rollback()
            write_private(dry, json.dumps(result, indent=2))
            return result | {"dry_run": True}
        expected = json.loads(dry.read_text())
        if expected["replay_id"] != state["replay_id"] or expected["deleted"] != result["deleted"]:
            conn.rollback()
            differ = f"the counts differ from the dry run's {expected['deleted']}"
            raise Refused(f"rolled back: {differ}")
    return result | {"dry_run": False, "go": args.owner_go}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database", required=True, help="credentials TOML of the database")
    parser.add_argument("--state", required=True, help="the replay's state.json")
    parser.add_argument("--apply", action="store_true", help="delete (after a dry run)")
    parser.add_argument("--owner-go", help=f"the owner's go for --apply: {GO_FORM}")
    parser.add_argument(target.ACKNOWLEDGE, action="store_true", help="the database is production")
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
