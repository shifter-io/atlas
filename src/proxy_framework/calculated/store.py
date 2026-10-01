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


def jsonable(value):
    return json.loads(json.dumps(value, default=iso, allow_nan=False))


def initialize(database):
    initialize_history(database)
    ddl = Path(__file__).with_name("schema.sql").read_text()
    checksum = hashlib.sha256(ddl.encode()).hexdigest()
    with connect(database) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(817452902)")
        if conn.execute("SELECT to_regclass('calculation_schema')").fetchone()[0]:
            if conn.execute("SELECT version,checksum FROM calculation_schema").fetchall() != [
                (1, checksum)
            ]:
                raise ValueError(
                    "Calculated schema checksum differs; an explicit migration is required"
                )
        conn.execute(ddl)
        conn.execute(
            "INSERT INTO calculation_schema VALUES (1,%s) ON CONFLICT DO NOTHING", (checksum,)
        )
    return {"calculated_schema_version": 1}


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


def validate_payload(payload):
    if payload["schema_version"] != 1 or not payload["methodology"]["id"]:
        raise ValueError("Invalid snapshot contract")
    for product in payload["products"]:
        for window in product["windows"].values():
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
