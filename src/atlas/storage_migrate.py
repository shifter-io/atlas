"""The steps of storage.migrate: each runs inside the one transaction migrate opens.

calculated.store imports storage, so the steps import it when they run, as migrate does.
"""

from __future__ import annotations

from pathlib import Path

from . import names

ROOT = Path(__file__).parent
# Each check counts rows whose rebuilt JSON differs from the v4 copy kept by the migration.
MIGRATION_CHECKS = {
    "pool definitions": "SELECT count(*) FROM v4_pools o FULL JOIN pool_details n USING(pool_id) "
    "WHERE o.definition::text IS DISTINCT FROM n.definition::text",
    "scopes and metrics": "SELECT count(*) FROM v4_scopes o FULL JOIN scope_details n "
    "USING(scope_id) WHERE (o.scope::text,o.metrics::text) "
    "IS DISTINCT FROM (n.scope::text,n.metrics::text)",
    "bucket counters": "SELECT count(*) FROM v4_buckets o FULL JOIN (SELECT run_key,bucket_start,"
    "run_bucket_counters(b) AS counters FROM run_buckets b) n USING(run_key,bucket_start) "
    "WHERE o.counters::text IS DISTINCT FROM n.counters::text",
    "run summaries": "SELECT count(*) FROM v4_summaries o FULL JOIN test_runs t USING(run_key) "
    "WHERE o.summary::text IS DISTINCT FROM stored_run_summary(t.run_key)::text",
}
# A v5 database is captured the same way before migrate_v5_v6.sql, then checked identically.
V5_SNAPSHOT = (
    "CREATE TEMP TABLE v5_before_pools ON COMMIT DROP AS "
    "SELECT pool_id,definition FROM pool_details;"
    "CREATE TEMP TABLE v5_before_scopes ON COMMIT DROP AS "
    "SELECT scope_id,scope,metrics FROM scope_details;"
    "CREATE TEMP TABLE v5_before_buckets ON COMMIT DROP AS "
    "SELECT run_key,bucket_start,run_bucket_counters(b) AS counters FROM run_buckets b;"
    "CREATE TEMP TABLE v5_before_summaries ON COMMIT DROP AS "
    "SELECT run_key,stored_run_summary(run_key) AS summary FROM test_runs"
)
V5_CHECKS = {name: query.replace("v4_", "v5_before_") for name, query in MIGRATION_CHECKS.items()}
# v6 → v7 changes test_batches only: every batch must be unchanged (and no batch a retry); v22
# adds paused_network, NULL on every stored batch.
V6_SNAPSHOT = "CREATE TEMP TABLE v6_before_batches ON COMMIT DROP AS SELECT * FROM test_batches"
V6_CHECKS = {
    "test batches": "SELECT count(*) FROM v6_before_batches o FULL JOIN test_batches n "
    "USING(batch_id) WHERE to_jsonb(o)::text IS DISTINCT FROM "
    "(to_jsonb(n) - 'retry_of' - 'paused_network')::text "
    "OR n.retry_of IS NOT NULL OR n.paused_network IS NOT NULL",
}


def read_versions(conn):
    """Lock both schemas and the planners; return the schema versions, the calculated layer
    (else None) and its versions (else None)."""
    conn.execute("SELECT pg_advisory_xact_lock(817452901)")
    conn.execute("SELECT pg_advisory_xact_lock(817452902)")
    conn.execute("SELECT pg_advisory_xact_lock(817452907)")  # batches.PLAN_LOCK
    if not conn.execute("SELECT to_regclass(current_schema||'.schema_versions')").fetchone()[0]:
        raise ValueError("No schema to migrate; use db-init")
    versions = conn.execute("SELECT version,checksum FROM schema_versions").fetchall()
    layered = conn.execute("SELECT to_regclass('calculation_schema')").fetchone()[0]
    layers = (
        conn.execute("SELECT version,checksum FROM calculation_schema").fetchall()
        if layered
        else None
    )
    return versions, layered, layers


def migratable_start(versions, migratable):
    """The version an older schema starts at; raise unless it is an unmodified migratable one."""
    start = migratable.get(versions[0] if len(versions) == 1 else None)
    if start is None:
        raise ValueError("Only an unmodified v4 to v19 schema can be migrated in place")
    return start


def layer_start(start, layered, layers, current_layer):
    """(upgraded, later): whether the calculated layer is current, else the calculated version
    after v2 it starts at (None from v2); raise unless migrate can upgrade it in place."""
    from .calculated import store

    # A v6 to v16 database may already carry the current calculated layer, and a v6 or
    # later one a calculated v3 to v6 layer (later is its version, else None).
    upgraded = layered and start >= 6 and layers == current_layer
    known = {(n, getattr(store, f"V{n}_CHECKSUM")): n for n in range(3, store.SCHEMA_VERSION)}
    later = known.get(layers[0]) if layered and start >= 6 and len(layers) == 1 else None
    expected = [(1, store.V1_CHECKSUM)] if start == 4 else [(2, store.V2_CHECKSUM)]
    if layered and not upgraded and not later and layers != expected:
        raise ValueError(
            "Only an unmodified calculated v1 schema can be migrated in place"
            if start == 4
            else "The calculated schema differs from this release; only an unmodified "
            "calculated v2 to v12 schema can be migrated in place"
        )
    return upgraded, later


def migrate_base(conn, start, layered, from_v2, schema_version):
    """Snapshot what the checks compare and run the base steps from start; return the checks.

    from_v2 is true when the calculated layer is neither current nor past v2: from a v4 database
    these steps migrate its v1 to v2, from a later one they snapshot its v2.
    """
    from .calculated import store

    checks = {}
    if start == 4:
        checks |= MIGRATION_CHECKS
        conn.execute((ROOT / "migrate_v4_v5.sql").read_text())
        if layered:
            conn.execute((ROOT / "calculated/migrate_v1_v2.sql").read_text())
            checks |= store.MIGRATION_CHECKS
    elif start == 5:
        checks |= V5_CHECKS
        conn.execute(V5_SNAPSHOT)
    if start > 4 and from_v2:
        checks |= store.V2_CHECKS
        conn.execute(store.V2_SNAPSHOT)
    if start == 6:
        checks |= V6_CHECKS
        conn.execute(V6_SNAPSHOT)
    for step in range(max(start, 5), schema_version):  # migrate_v5_v6.sql .. v21_v22.sql
        conn.execute((ROOT / f"migrate_v{step}_v{step + 1}.sql").read_text())
    if start < 9:
        checks |= names.V8_CHECKS
    return checks


def migrate_calculated(conn, start, stale, later):
    """On a stale (not current) calculated layer, run v2 → v3 unless it starts later, then each
    step from later (else v3) to the current version; return the names of the steps' checks."""
    from .calculated import store

    if stale and not later:
        conn.execute((ROOT / "calculated/migrate_v2_v3.sql").read_text())
    steps = []
    for n in range(3, store.SCHEMA_VERSION):  # calculated v3 → v4 up to v8 → v9, from its step
        if stale and (later or n) <= n:
            conn.execute(getattr(store, f"V{n}_SNAPSHOT"))
            conn.execute((ROOT / f"calculated/migrate_v{n}_v{n + 1}.sql").read_text())
            # Checked at once: they compare stored rows, which a later step may change.
            steps += verify(conn, getattr(store, f"V{n}_CHECKS"), start)
    return steps


def verify(conn, checks, start):
    """Raise unless every check counts no differing row; return the checks' names."""
    failed = {
        name: count
        for name, query in checks.items()
        if (count := conn.execute(query).fetchone()[0])
    }
    if failed:
        raise ValueError(f"Migrated rows do not rebuild their v{start} JSON: {failed}")
    return list(checks)


def record(conn, version, layer):
    """Record the new schema version and calculated layer, each (version, checksum) or None
    when it is unchanged."""
    if version:
        conn.execute("DELETE FROM schema_versions")
        conn.execute("INSERT INTO schema_versions(version,checksum) VALUES (%s,%s)", version)
    if layer:
        conn.execute("DELETE FROM calculation_schema")
        conn.execute("INSERT INTO calculation_schema VALUES (%s,%s)", layer)


def compacted(start, layered, upgraded):
    """The tables to compact after migrating from start (VACUUM FULL, outside the transaction).

    Dropped columns and updated rows keep their bytes until the tables are compacted: the v4
    columns, the v8 names, and calculated v6's ASN names and renumbered breakdown places.
    v5 → v6 to v17 → v18 (but v8 → v9) and calculated v2 → v3 only add tables (v12 → v13,
    v13 → v14 and v15 → v16 fill their new one), functions, a trigger, nullable or
    constant-default columns and indexes (and swap a check constraint or index), calculated
    v12 → v13 drops a foreign key, v13 → v14 and v14 → v15 add nullable columns, v15 → v16
    empty tables and a nullable column, v16 → v17 and v17 → v18 an empty table, calculated
    v18 → v19 a small table filled from the published heads, calculated v19 → v20 empty
    tables and an index, calculated v20 → v21 a constant-default column of the small table, and
    calculated v22 → v23 drops two foreign keys and calculated v23 → v24 adds a nullable column
    and an empty table: no rewrite. v18 → v19 updates some rows of the
    small country_verdicts table (one row a daily test): nothing worth compacting. v19 → v20
    drops a column of that small table, v20 → v21 swaps a check constraint and v21 → v22 adds a
    nullable column: no rewrite.
    """
    tables = ["enrichment_locations", "enrichment_networks"] if start < 9 else []
    if start == 4:
        tables += ["pool_stats", "scope_stats", "test_runs", "run_buckets"]
    if start == 4 and layered:
        tables += ["calculated_windows", "calculated_scores"]
    if layered and not upgraded:
        tables += ["asn_labels"]  # calculated v9 drops calculated_breakdowns
    return tables
