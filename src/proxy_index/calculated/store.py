"""Publication and read-only snapshot access; no history queries on the read path."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path

from psycopg import sql
from psycopg.rows import tuple_row

from ..storage import connect
from ..storage import initialize as initialize_history
from .aggregate import EVIDENCE_RANGES, iso
from .policy import digest

SCHEMA_VERSION = 6
# Checksums of the unmodified v1 and v2 calculated schemas that storage.migrate() upgrades in place.
V1_CHECKSUM = "2d4157aeadac7da566ab1edde723ad425cb068318c4a3722ca94f3c57d9f68a0"
V2_CHECKSUM = "19bf2db0cd112b87d2de06aa0156d2693f36d8e21c2c2b1215ecc00ccb7f7699"
V3_CHECKSUM = "5f24879fc7f37486903881c91f5334f85cfa83dcffd90fc00a972508424b394d"
V4_CHECKSUM = "83d3794413bb56b371ff04ac7f9978652310b20013077c0400d1477d7fb24938"
V5_CHECKSUM = "ccb566016dba5d6d18b09da7cb88ced16758f0fbf446fcac88495d67d94b8782"
# Unpublished, non-current builds keep their payload only while among the newest few drafts.
KEEP_DRAFTS = 2
# After each publication expire() keeps the current build, the newest RETAIN_PER_DAY published
# builds of each of the last RETAIN_DAYS UTC days and the drafts whose payload prune() kept.
RETAIN_DAYS = 30
RETAIN_PER_DAY = 1
# Rows of a build in the tables keyed by its windows' result_id; breakdowns live in the build's
# partition (calculated_breakdowns_b<build_id>).
DERIVED_TABLES = (
    "calculated_score_reasons",
    "calculated_score_components",
    "calculated_scores",
    "calculated_series",
)
# Test kinds of the snapshot's traffic.by_test (load since the latency-under-load test).
TESTS = {"census", "pulse", "load"}
MIGRATION_CHECKS = {
    "cohorts and readiness": "SELECT count(*) FROM v1_windows o JOIN calculated_windows w "
    "USING(result_id) LEFT JOIN cohort_criteria c ON c.cohort=w.cohort WHERE "
    "(o.metadata->'criteria')::text IS DISTINCT FROM c.criteria::text "
    "OR o.metadata->>'readiness' IS DISTINCT FROM w.readiness",
    "breakdowns": "SELECT count(*) FROM v1_breakdowns o FULL JOIN calculated_breakdown_details n "
    "ON (o.result_id,o.dimension,o.label::text)=(n.result_id,n.dimension,n.label::text) "
    "WHERE (o.ips,o.observations) IS DISTINCT FROM (n.ips,n.observations)",
    "breakdown order": "SELECT count(*) FROM v1_positions o "
    "FULL JOIN calculated_breakdown_details n "
    "ON (o.result_id,o.dimension,o.position)=(n.result_id,n.dimension,n.position) "
    "WHERE o.label::text IS DISTINCT FROM n.label::text",
    "score components and reasons": "SELECT count(*) FROM v1_scores o FULL JOIN v1_weights w "
    "USING(result_id,profile) FULL JOIN calculated_score_details n USING(result_id,profile) WHERE "
    "(o.components::text,o.reasons::text,w.weights::text) "
    "IS DISTINCT FROM (n.components::text,n.reasons::text,n.weights::text)",
}

# A v2 layer is captured before migrate_v2_v3.sql; its windows, breakdowns and scores must rebuild
# unchanged afterwards (the new columns are NULL on every migrated row).
V2_SNAPSHOT = (
    "CREATE TEMP TABLE v2_before_windows ON COMMIT DROP AS SELECT * FROM calculated_windows;"
    "CREATE TEMP TABLE v2_before_breakdowns ON COMMIT DROP AS "
    "SELECT result_id,dimension,position,ips,observations,label FROM calculated_breakdown_details;"
    "CREATE TEMP TABLE v2_before_scores ON COMMIT DROP AS "
    "SELECT result_id,profile,components,weights,reasons FROM calculated_score_details"
)
V2_CHECKS = {
    "v2 windows": "SELECT count(*) FROM v2_before_windows o FULL JOIN calculated_windows n "
    "USING(result_id) WHERE to_jsonb(o)::text IS DISTINCT FROM (to_jsonb(n) - ARRAY["
    "'challenge_rate','block_rate','uptime','bytes_up','bytes_down'])::text",
    "v2 breakdowns": "SELECT count(*) FROM v2_before_breakdowns o FULL JOIN "
    "calculated_breakdown_details n USING(result_id,dimension,position) WHERE "
    "(o.ips,o.observations,o.label::text) IS DISTINCT FROM (n.ips,n.observations,n.label::text) "
    "OR num_nonnulls(n.completed,n.successes,n.challenges,n.blocks,n.p50,n.p90,n.p99)>0",
    "v2 scores": "SELECT count(*) FROM v2_before_scores o FULL JOIN calculated_score_details n "
    "USING(result_id,profile) WHERE (o.components::text,o.weights::text,o.reasons::text) "
    "IS DISTINCT FROM (n.components::text,n.weights::text,n.reasons::text)",
}
V3_SNAPSHOT = "CREATE TEMP TABLE v3_before_series ON COMMIT DROP AS SELECT * FROM calculated_series"
V3_CHECKS = {
    "availability history": "SELECT count(*) FROM v3_before_series o FULL JOIN calculated_series n "
    "USING(result_id,bucket_start) WHERE to_jsonb(o)::text IS DISTINCT FROM "
    "(to_jsonb(n) - 'uptime')::text OR n.uptime IS NOT NULL",
}
V4_SNAPSHOT = (
    "CREATE TEMP TABLE v4_before_breakdowns ON COMMIT DROP AS "
    "SELECT * FROM calculated_breakdowns;"
    "CREATE TEMP TABLE v4_before_asn_labels ON COMMIT DROP AS SELECT * FROM asn_labels"
)
V4_CHECKS = {
    "location network evidence": "SELECT count(*) FROM v4_before_breakdowns o FULL JOIN "
    "calculated_breakdowns n USING(result_id,dimension,position) WHERE "
    "to_jsonb(o)::text IS DISTINCT FROM (to_jsonb(n) - 'build_id')::text",
    "network labels": "SELECT count(*) FROM v4_before_asn_labels o FULL JOIN asn_labels n "
    "USING(asn_label_id) WHERE to_jsonb(o)::text IS DISTINCT FROM to_jsonb(n)::text",
}

# A v5 layer's breakdowns move to per-build partitions; each must keep its values and gain the
# build of its window.
V5_SNAPSHOT = (
    "CREATE TEMP TABLE v5_before_breakdowns ON COMMIT DROP AS SELECT * FROM calculated_breakdowns"
)
V5_CHECKS = {
    "partitioned breakdowns": "SELECT count(*) FROM v5_before_breakdowns o FULL JOIN "
    "calculated_breakdowns n USING(result_id,dimension,position) LEFT JOIN calculated_windows w "
    "ON w.result_id=n.result_id WHERE to_jsonb(o)::text IS DISTINCT FROM "
    "(to_jsonb(n) - 'build_id')::text OR n.build_id IS DISTINCT FROM w.build_id",
}


def jsonable(value):
    return json.loads(json.dumps(value, default=iso, allow_nan=False))


def schema():
    ddl = Path(__file__).with_name("schema.sql").read_text()
    return ddl, hashlib.sha256(ddl.encode()).hexdigest()


def initialize(database):
    initialize_history(database)
    ddl, checksum = schema()
    with connect(database) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(817452902)")
        if conn.execute("SELECT to_regclass('calculation_schema')").fetchone()[0]:
            if conn.execute("SELECT version,checksum FROM calculation_schema").fetchall() != [
                (SCHEMA_VERSION, checksum)
            ]:
                raise ValueError(
                    "Calculated schema checksum differs; an explicit migration is required"
                )
        conn.execute(ddl)
        conn.execute(
            "INSERT INTO calculation_schema VALUES (%s,%s) ON CONFLICT DO NOTHING",
            (SCHEMA_VERSION, checksum),
        )
    return {"calculated_schema_version": SCHEMA_VERSION}


def prune(conn):
    """Drop payloads of unpublished, non-current builds beyond the newest KEEP_DRAFTS."""
    return conn.execute(
        "DELETE FROM calculated_snapshots WHERE build_id IN (SELECT s.build_id FROM "
        "calculated_snapshots s JOIN calculation_builds b USING(build_id) WHERE NOT b.published "
        "AND s.build_id NOT IN (SELECT build_id FROM current_calculation) "
        "ORDER BY s.build_id DESC OFFSET %s)",
        (KEEP_DRAFTS,),
    ).rowcount


def partition(conn, build_id):
    """Create the build's breakdown partition (calculated_breakdowns_b<build_id>)."""
    conn.execute(
        sql.SQL(
            "CREATE TABLE IF NOT EXISTS {} PARTITION OF calculated_breakdowns FOR VALUES IN ({})"
        ).format(
            sql.Identifier(f"calculated_breakdowns_b{int(build_id)}"), sql.Literal(int(build_id))
        )
    )


def discard(conn, build_id):
    """Delete one build and all of its rows; its breakdowns go with their partition.

    The current build cannot be discarded: current_calculation references its snapshot. On a
    calculated v5 database (no partitions yet) the breakdowns are deleted through their windows.
    """
    conn.execute(
        sql.SQL("DROP TABLE IF EXISTS {}").format(
            sql.Identifier(f"calculated_breakdowns_b{int(build_id)}")
        )
    )
    for table in ("calculated_breakdowns", *DERIVED_TABLES):
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
        discard(conn, build_id)
    return {"kept_builds": kept, "expired_builds": expired}


def estimate(conn, expired):
    """Rows and approximate bytes that expiring these builds frees (read-only).

    Breakdowns are counted through their windows, so this also works on a calculated v5
    database before its migration. Bytes are each table's size (with its partitions, indexes
    and TOAST) in proportion to the rows removed; payloads use their stored size.
    """
    windows = "result_id IN (SELECT result_id FROM calculated_windows WHERE build_id=ANY(%s))"
    keys = {table: windows for table in (*DERIVED_TABLES, "calculated_breakdowns")} | {
        table: "build_id=ANY(%s)"
        for table in ("calculated_snapshots", "calculated_windows", "calculated_overlaps")
    }
    rows, size = {}, {}
    for table, key in keys.items():
        rows[table] = conn.execute(
            f"SELECT count(*) FROM {table} WHERE {key}", (expired,)
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
    return {"rows": rows, "bytes": size, "total_bytes": sum(size.values())}


def retention(database, days=None, per_day=None, *, dry_run=False):
    """Apply the retention rule now (proxy-index calculate-expire); dry_run only reports it."""
    with connect(database) as conn:
        if dry_run:
            conn.execute("SET TRANSACTION READ ONLY")
            kept, expired = retained(conn, days, per_day)
            return {"kept_builds": kept, "expired_builds": expired, "dry_run": True} | estimate(
                conn, expired
            )
        conn.execute("SELECT pg_advisory_xact_lock(817452902)")
        # A v5 database can expire before db-migrate, so that the migration moves only kept rows.
        layers = {(SCHEMA_VERSION, schema()[1]), (5, V5_CHECKSUM)}
        if conn.execute("SELECT version,checksum FROM calculation_schema").fetchall() not in [
            [layer] for layer in layers
        ]:
            raise ValueError("Unknown calculated schema; run db-migrate before expiring builds")
        return expire(conn, days, per_day)


def assign_role(database, run_id, role):
    if role not in {"exploratory", "pool_sampling", "benchmark", "monitor"}:
        raise ValueError("Unknown collection role")
    with connect(database) as conn:
        row = conn.execute("SELECT run_key FROM test_runs WHERE run_id=%s", (run_id,)).fetchone()
        if not row:
            raise ValueError("Unknown run")
        conn.execute(
            "INSERT INTO calculation_roles VALUES (%s,%s) ON CONFLICT(run_key) "
            "DO UPDATE SET role=excluded.role",
            (row[0], role),
        )
    return {"run_id": str(run_id), "role": role}


def evidence(database, record):
    """Store one metric evidence record (or a list of them, in one transaction).

    A record names its product by product_id, or portably by provider and product (resolved
    here, so the product must exist). cohort "*" applies to every cohort of the product
    (product-wide facts such as list prices). Replays of the same content are ignored.
    """
    if isinstance(record, list):
        if not record:
            raise ValueError("No evidence records")
        with connect(database) as conn:
            return [_evidence(conn, item) for item in record]
    with connect(database) as conn:
        return _evidence(conn, record)


def _evidence(conn, record):
    expected = {
        "cohort",
        "metric",
        "value",
        "period_start",
        "period_end",
        "valid_until",
        "source",
    }
    if not isinstance(record, dict):
        raise ValueError("Evidence requires a known metric and the documented fields")
    names = set(record) - expected
    if names not in ({"product_id"}, {"provider", "product"}) or record.get("metric") not in (
        EVIDENCE_RANGES
    ):
        raise ValueError("Evidence requires a known metric and the documented fields")
    lo, hi = EVIDENCE_RANGES[record["metric"]]
    value = record["value"]
    if type(value) not in (int, float) or not math.isfinite(value) or not lo <= value <= hi:
        raise ValueError("Invalid evidence value")
    if not record["source"].strip() or not record["cohort"].strip():
        raise ValueError("Evidence needs provenance and a comparable cohort")
    times = [
        datetime.fromisoformat(record[k]) for k in ("period_start", "period_end", "valid_until")
    ]
    if any(t.tzinfo is None for t in times) or not times[0] < times[1] <= times[2]:
        raise ValueError("Invalid evidence interval")
    if "product_id" not in record:
        row = conn.execute(
            "SELECT product_id FROM products WHERE provider=%s AND name=%s",
            (record["provider"], record["product"]),
        ).fetchone()
        if row is None:
            raise ValueError("Unknown provider product")
        record = {k: v for k, v in record.items() if k not in ("provider", "product")} | {
            "product_id": row[0]
        }
    conn.execute(
        "INSERT INTO metric_evidence(product_id,cohort,metric,value,period_start,"
        "period_end,valid_until,source,content_hash) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT(content_hash) DO NOTHING",
        (
            *[record[k] for k in ("product_id", "cohort", "metric", "value")],
            *times,
            record["source"],
            digest(record),
        ),
    )
    return {"content_hash": digest(record)}


def head(database, snapshot="current"):
    with connect(database) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        clause = (
            "build_id=(SELECT build_id FROM current_calculation WHERE singleton)"
            if snapshot == "current"
            else "build_id=%s"
        )
        row = conn.execute(
            "SELECT build_id,content_hash FROM calculated_snapshots WHERE " + clause,
            () if snapshot == "current" else (int(snapshot),),
        ).fetchone()
    return row


def read(database, snapshot="current"):
    # Exactly one prepared row, even for API projections. Never join raw-history tables here.
    with connect(database) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        query = (
            "SELECT payload FROM calculated_snapshots WHERE build_id="
            "(SELECT build_id FROM current_calculation WHERE singleton)"
            if snapshot == "current"
            else "SELECT payload FROM calculated_snapshots WHERE build_id=%s"
        )
        row = conn.execute(query, () if snapshot == "current" else (int(snapshot),)).fetchone()
    return row[0] if row else None


def _fraction(value):
    return value is None or (type(value) in (int, float) and 0 <= value <= 1)


def validate_payload(payload):
    """Check the reconciling invariants of a v1 or v2 snapshot before it is stored or published."""
    version = payload["schema_version"]
    if version not in (1, 2) or not payload["methodology"]["id"]:
        raise ValueError("Invalid snapshot contract")
    if version == 2 and (
        not isinstance(payload["events"], list)
        or not {"census", "pulse"} <= set(payload["traffic"]["by_test"]) <= TESTS
    ):
        raise ValueError("Invalid snapshot contract")
    for product in payload["products"]:
        if version == 2 and not isinstance(product["census"], list):
            raise ValueError("Invalid snapshot contract")
        for window in product["windows"].values():
            if any(not _fraction(point.get("uptime")) for point in window["series"]):
                raise ValueError("Availability series must contain fractions or null")
            if version == 2:
                for rows in window["breakdowns"].values():
                    for row in rows:
                        if not 0 <= row["successes"] <= row["completed"]:
                            raise ValueError("Breakdown counters do not reconcile")
                if not all(
                    _fraction(window["metrics"][k])
                    for k in ("challenge_rate", "block_rate", "uptime")
                ):
                    raise ValueError("Snapshot rates must be fractions")
            for m in [
                window["metrics"],
                window["benchmark"],
                *[t["metrics"] for t in window["targets"].values()],
            ]:
                if (
                    m["completed"] != m["successes"] + m["errors"]
                    or min(m["completed"], m["successes"], m["errors"]) < 0
                ):
                    raise ValueError("Snapshot counters do not reconcile")
            if window["pool"]["observations"] > window["metrics"]["successes"]:
                raise ValueError("IP observations exceed successful requests")
            for score in window["scores"].values():
                if score["state"] == "ready" and score["score"] is None:
                    raise ValueError("Ready score is missing")
    json.dumps(payload, allow_nan=False)


def publish(database, snapshot):
    with connect(database) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(817452902)")
        row = conn.execute(
            "SELECT payload FROM calculated_snapshots WHERE build_id=%s", (int(snapshot),)
        ).fetchone()
        if not row:
            raise ValueError("Unknown snapshot")
        validate_payload(row[0])
        current = conn.execute(
            "SELECT b.as_of FROM current_calculation c JOIN calculation_builds b USING(build_id)"
        ).fetchone()
        if current and datetime.fromisoformat(row[0]["as_of"]) < current[0]:
            raise ValueError("Cannot publish a snapshot older than the current measurement cutoff")
        conn.execute(
            "INSERT INTO current_calculation VALUES (true,%s) ON CONFLICT(singleton) "
            "DO UPDATE SET build_id=excluded.build_id",
            (int(snapshot),),
        )
        conn.execute(
            "UPDATE calculation_builds SET published=true WHERE build_id=%s", (int(snapshot),)
        )
        prune(conn)
        expired = expire(conn)["expired_builds"]
    return {"published_snapshot": str(snapshot), "expired_builds": expired}


def export(database, path, snapshot="current"):
    payload = read(database, snapshot)
    if payload is None:
        raise ValueError("No published snapshot")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)
    return {"snapshot_id": payload["snapshot_id"], "path": str(path)}
