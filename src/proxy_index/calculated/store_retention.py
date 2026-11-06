"""Build retention (proxy-index calculate-expire, and expire() after each publication): which
builds the rule keeps, what expiring the others frees, and deleting a build with its rows. store.py
re-exports every name, so callers keep importing them from there."""

from __future__ import annotations

from psycopg import sql
from psycopg.rows import tuple_row

from ..storage import connect
from . import netlists
from .store_steps import SCHEMA_VERSION, V5_CHECKSUM, schema

# After each publication expire() keeps the current build, the newest RETAIN_PER_DAY published
# builds of each of the last RETAIN_DAYS UTC days and the drafts whose payload prune() kept.
RETAIN_DAYS = 30
RETAIN_PER_DAY = 1
# Rows of a build in the tables keyed by its windows' result_id; snapshot parts live in the
# build's partition (calculated_snapshot_parts_b<build_id>), breakdowns in shared lists (K2).
DERIVED_TABLES = (
    "calculated_score_reasons",
    "calculated_score_components",
    "calculated_scores",
    "calculated_series",
)


def drop(conn, build_id, table):
    """Drop the build's partition of table, if it has one."""
    name = sql.Identifier(f"{table}_b{int(build_id)}")
    conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(name))


def lists(conn):
    """Whether the database has the shared lists (calculated v9); a v5 layer has not."""
    query = "SELECT to_regclass('calculated_lists') IS NOT NULL"
    return conn.cursor(row_factory=tuple_row).execute(query).fetchone()[0]


# Group lists live while a window references them, network lists while their build has windows.
collect = netlists.collect


def discard(conn, build_id, *, gc=True):
    """Delete one build and all of its rows; its parts go with their partition, and (gc) the
    lists no other build's window references are collected.

    The current build cannot be discarded: current_calculation references its snapshot. On a
    calculated v5 database (no lists yet) the breakdowns are deleted through their windows.
    """
    shared = lists(conn)
    drop(conn, build_id, "calculated_snapshot_parts")
    for table in (*(() if shared else ("calculated_breakdowns",)), *DERIVED_TABLES):
        conn.execute(
            f"DELETE FROM {table} WHERE result_id IN "
            "(SELECT result_id FROM calculated_windows WHERE build_id=%s)",
            (build_id,),
        )
    for table in (
        "calculated_snapshots",
        "calculated_windows",
        "calculated_overlaps",
        "calculation_builds",
    ):
        conn.execute(f"DELETE FROM {table} WHERE build_id=%s", (build_id,))
    if shared and gc:
        collect(conn)


def retained(conn, days=None, per_day=None):
    """Return (kept, expired) build ids under the retention rule (see RETAIN_DAYS).

    days and per_day default to RETAIN_DAYS and RETAIN_PER_DAY. Days count back from the UTC
    day of the current build's cutoff, not from the clock, so a paused calculator never expires
    history; the current day is the first of the days. Without a current build nothing expires.
    """
    days = RETAIN_DAYS if days is None else days
    per_day = RETAIN_PER_DAY if per_day is None else per_day
    if type(days) is not int or type(per_day) is not int or min(days, per_day) < 0:
        raise ValueError("Retention days and builds per day must be non-negative integers")
    # Tuple rows even inside calculate(), whose connection reads dicts.
    cursor = conn.cursor(row_factory=tuple_row)
    cursor.execute(
        "WITH cur AS (SELECT b.build_id,(b.as_of AT TIME ZONE 'UTC')::date AS day "
        "FROM current_calculation c JOIN calculation_builds b USING(build_id)), "
        "daily AS (SELECT b.build_id,row_number() OVER (PARTITION BY "
        "(b.as_of AT TIME ZONE 'UTC')::date ORDER BY b.as_of DESC,b.build_id DESC) AS n "
        "FROM calculation_builds b,cur WHERE b.published "
        "AND (b.as_of AT TIME ZONE 'UTC')::date>cur.day-%s::integer) "
        "SELECT b.build_id,b.build_id IN (SELECT build_id FROM cur) "
        "OR b.build_id IN (SELECT build_id FROM daily WHERE n<=%s) "
        "OR NOT b.published AND b.build_id IN (SELECT build_id FROM calculated_snapshots) "
        "FROM calculation_builds b WHERE EXISTS (SELECT FROM cur) ORDER BY 1",
        (days, per_day),
    )
    rows = cursor.fetchall()
    return [b for b, keep in rows if keep], [b for b, keep in rows if not keep]


def expire(conn, days=None, per_day=None):
    """Delete every build outside the retention rule; return the kept and expired build ids."""
    kept, expired = retained(conn, days, per_day)
    for build_id in expired:
        discard(conn, build_id, gc=False)
    if expired and lists(conn):
        collect(conn)
    return {"kept_builds": kept, "expired_builds": expired}


def estimate(conn, expired):
    """Rows and approximate bytes that expiring these builds frees (read-only).

    Lists count when no kept window references them, network lists when their build expires; on
    a calculated v5 database, before its migration, breakdowns count through their windows.
    Bytes are each table's size (with its partitions, indexes and TOAST) in proportion to the
    rows removed; payloads and their parts use their stored size.
    """
    windows = "result_id IN (SELECT result_id FROM calculated_windows WHERE build_id=ANY(%(b)s))"
    freed = (
        "list_id IN (SELECT list_id FROM calculated_windows WHERE build_id=ANY(%(b)s) "
        "EXCEPT SELECT list_id FROM calculated_windows WHERE build_id<>ALL(%(b)s) "
        "UNION SELECT list_id FROM calculated_lists "
        "WHERE cohort='network' AND created_build=ANY(%(b)s))"
    )
    shared = {"calculated_list_rows": freed, "calculated_lists": freed}
    keys = {table: windows for table in DERIVED_TABLES} | {
        table: "build_id=ANY(%(b)s)"
        for table in ("calculated_snapshots", "calculated_windows", "calculated_overlaps")
    }
    keys |= shared if lists(conn) else {"calculated_breakdowns": windows}
    rows, size = {}, {}
    for table, key in keys.items():
        rows[table] = conn.execute(
            f"SELECT count(*) FROM {table} WHERE {key}", {"b": expired}
        ).fetchone()[0]
        total, stored = conn.execute(
            f"SELECT (SELECT count(*) FROM {table}),pg_total_relation_size(%s::regclass)"
            "+coalesce((SELECT sum(pg_total_relation_size(inhrelid)) FROM pg_inherits "
            "WHERE inhparent=%s::regclass),0)",
            (table, table),
        ).fetchone()
        size[table] = int(stored * rows[table] / total) if total else 0
    size["calculated_snapshots"] = conn.execute(
        "SELECT coalesce(sum(pg_column_size(payload)),0) FROM calculated_snapshots "
        "WHERE build_id=ANY(%s)",
        (expired,),
    ).fetchone()[0]
    if conn.execute("SELECT to_regclass('calculated_snapshot_parts')").fetchone()[0]:
        rows["calculated_snapshot_parts"], size["calculated_snapshot_parts"] = conn.execute(
            "SELECT count(*),coalesce(sum(pg_column_size(part)),0) FROM calculated_snapshot_parts "
            "WHERE build_id=ANY(%s)",
            (expired,),
        ).fetchone()
    return {"rows": rows, "bytes": size, "total_bytes": sum(size.values())}


def layer(conn):
    """Name the calculated layer that retention accepts ("v9" or an unmodified "v5"), or raise.

    A v5 database can expire before db-migrate, so that the migration moves only kept rows.
    """
    layers = {(SCHEMA_VERSION, schema()[1]): f"v{SCHEMA_VERSION}", (5, V5_CHECKSUM): "v5"}
    rows = conn.execute("SELECT version,checksum FROM calculation_schema").fetchall()
    if len(rows) != 1 or tuple(rows[0]) not in layers:
        raise ValueError("Unknown calculated schema; run db-migrate before expiring builds")
    return layers[tuple(rows[0])]


def retention(database, days=None, per_day=None, *, dry_run=False):
    """Apply the retention rule now (proxy-index calculate-expire); dry_run only reports it.

    Both check the calculated layer first and report it as "schema".
    """
    with connect(database) as conn:
        if dry_run:
            conn.execute("SET TRANSACTION READ ONLY")
        else:
            conn.execute("SELECT pg_advisory_xact_lock(817452902)")
        found = {"schema": layer(conn)}
        if not dry_run:
            return found | expire(conn, days, per_day)
        kept, expired = retained(conn, days, per_day)
        report = {"kept_builds": kept, "expired_builds": expired, "dry_run": True}
        return found | report | estimate(conn, expired)
