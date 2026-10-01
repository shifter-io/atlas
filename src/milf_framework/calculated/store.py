"""Publication and read-only snapshot access; no history queries on the read path."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path

from ..storage import connect
from ..storage import initialize as initialize_history
from .aggregate import EVIDENCE_RANGES, iso
from .policy import digest

SCHEMA_VERSION = 3
# Checksums of the unmodified v1 and v2 calculated schemas that storage.migrate() upgrades in place.
V1_CHECKSUM = "2d4157aeadac7da566ab1edde723ad425cb068318c4a3722ca94f3c57d9f68a0"
V2_CHECKSUM = "19bf2db0cd112b87d2de06aa0156d2693f36d8e21c2c2b1215ecc00ccb7f7699"
# Unpublished, non-current builds keep their payload only while among the newest few drafts.
KEEP_DRAFTS = 2
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
    expected = {
        "product_id",
        "cohort",
        "metric",
        "value",
        "period_start",
        "period_end",
        "valid_until",
        "source",
    }
    if set(record) != expected or record["metric"] not in EVIDENCE_RANGES:
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
    with connect(database) as conn:
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
        or set(payload["traffic"]["by_test"]) != {"census", "pulse"}
    ):
        raise ValueError("Invalid snapshot contract")
    for product in payload["products"]:
        if version == 2 and not isinstance(product["census"], list):
            raise ValueError("Invalid snapshot contract")
        for window in product["windows"].values():
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
    return {"published_snapshot": str(snapshot)}


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
