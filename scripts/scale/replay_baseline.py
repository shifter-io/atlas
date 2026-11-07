"""REPLAY-1: what the database held before the replay, and which rows the replay added
(REPLAY.md, "Telling replay rows from the database's own")."""

from __future__ import annotations

from datetime import UTC, datetime

from psycopg import sql
from target import REPLAY_MARKER

TABLES = (
    "SELECT n.nspname,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
    "WHERE n.nspname IN ('public','site') AND c.relkind IN ('r','p') AND NOT c.relispartition "
    "ORDER BY 1,2"
)
IDENTITIES = (
    "SELECT table_schema,table_name,column_name FROM information_schema.columns "
    "WHERE table_schema IN ('public','site') AND is_identity='YES' ORDER BY 1,2,3"
)
COLUMNS = (
    "SELECT table_name,column_name FROM information_schema.columns WHERE table_schema='public' "
    "AND column_name IN ('run_key','node_id') ORDER BY 1,2"
)
PRIMARY = (
    "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid=i.indrelid "
    "AND a.attnum=ANY(i.indkey) WHERE i.indrelid=%s::regclass AND i.indisprimary "
    "ORDER BY array_position(i.indkey::int2[],a.attnum)"
)
SMALL = ("providers", "products", "pool_stats", "scope_stats", "nodes", "targets", "dbip_releases")
SMALL += ("place_names", "asn_names", "asn_labels", "enrichment_locations", "enrichment_networks")
SMALL += ("cohorts",)  # for WIPE-TOOL-1: its keys are text
MARKED = sql.SQL("summary ? {}").format(sql.Literal(REPLAY_MARKER))
RUNS = sql.SQL("run_key IN (SELECT run_key FROM public.test_runs WHERE {})").format(MARKED)
BATCHES = sql.SQL(
    "batch_id IN (SELECT batch_id FROM public.batch_runs WHERE node_id=ANY(%(nodes)s))"
)


def scalar(conn, query):
    return conn.execute(query).fetchone()[0]


def counts(conn) -> dict[str, int]:
    """The rows of every table in public and site (a partitioned table with its partitions)."""
    found = {}
    for schema, name in conn.execute(TABLES).fetchall():
        query = sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(schema, name))
        found[f"{schema}.{name}"] = scalar(conn, query)
    return found


def baseline(conn) -> dict:
    """The counts, the highest value of every identity column and the small tables' keys."""
    known, identities, keys = counts(conn), {}, {}
    for schema, name, column in conn.execute(IDENTITIES).fetchall():
        if f"{schema}.{name}" in known:  # not a partition
            table = sql.Identifier(schema, name)
            query = sql.SQL("SELECT max({}) FROM {}").format(sql.Identifier(column), table)
            identities[f"{schema}.{name}.{column}"] = scalar(conn, query)
    for name in (name for name in SMALL if f"public.{name}" in known):
        columns = [row[0] for row in conn.execute(PRIMARY, (f"public.{name}",)).fetchall()]
        query = sql.SQL("SELECT {} FROM {}").format(
            sql.SQL(",").join(map(sql.Identifier, columns)), sql.Identifier("public", name)
        )
        keys[name] = sorted(list(row) for row in conn.execute(query).fetchall())
    at = datetime.now(UTC).isoformat(timespec="seconds")
    return {"at": at, "counts": known, "identities": identities, "keys": keys}


def day_counts(conn, before: dict[str, int], nodes: list[int]) -> dict:
    """The counts after a day; per table its growth, the rows reachable from the replay's runs
    (by run_key) and nodes (by node_id), and the other growth: production's own."""
    after, where = counts(conn), {"test_runs": MARKED, "test_batches": BATCHES}
    for name, column in conn.execute(COLUMNS).fetchall():  # node_id sorts after run_key
        if f"public.{name}" in after and (column == "node_id" or name not in where):
            where[name] = sql.SQL("node_id=ANY(%(nodes)s)") if column == "node_id" else RUNS
    replay = {}
    for name, condition in sorted(where.items()):
        table = sql.Identifier("public", name)
        query = sql.SQL("SELECT count(*) FROM {} WHERE {}").format(table, condition)
        replay[f"public.{name}"] = conn.execute(query, {"nodes": nodes}).fetchone()[0]
    growth = {name: after[name] - before.get(name, 0) for name in after}
    other = {name: growth[name] - replay.get(name, 0) for name in after}
    return {"counts": after, "growth": growth, "replay": replay, "other": other}
