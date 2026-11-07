"""Transactional, replay-safe PostgreSQL ingestion of durable measurement artifacts."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from itertools import accumulate
from pathlib import Path

import psycopg as psycopg  # re-exported: scripts catch storage.psycopg errors
from psycopg.types.json import Jsonb

from . import storage_ingest, storage_migrate
from .partitions import RAW_DAYS as RAW_DAYS  # re-exported for the calculator
from .partitions import partition as partition
from .stats import Histogram
from .storage_ingest import ADAPTER as ADAPTER
from .storage_ingest import _profile, column
from .storage_migrate import MIGRATION_CHECKS as MIGRATION_CHECKS
from .storage_migrate import V6_CHECKS as V6_CHECKS
from .storage_read import ENRICHMENT_FIELDS as ENRICHMENT_FIELDS
from .storage_read import GEO_COUNTERS as GEO_COUNTERS
from .storage_read import OUTCOME_KEYS as OUTCOME_KEYS
from .storage_read import _bucket as _bucket
from .storage_read import _read_run, _steps
from .storage_read import connect as connect
from .storage_read import read_summary as read_summary
from .storage_seed import seed as seed
from .storage_status import status as status

SCHEMA_VERSION = 12
# Only an unmodified v4 to v11 schema (these checksums) is migrated in place; migrate().
V4_CHECKSUM = "0ec5b6579c67bf883dead3e64312899dc6665cdb684ac617f06edb6d9cf1fee5"
V5_CHECKSUM = "2e1d8e89ffcf0748a882e7c4cddfd3e33e86a0024cc757648a4cabb502e64e3b"
V6_CHECKSUM = "1a06ee59c45bcaaaf14b11db107890a94dfc41b9f3b823472f7c8a22c984bc10"
V7_CHECKSUM = "75eb7782f9700e6b57b42d59ef8ffd1f46976d9178432689131c0add5ae5f0a5"
V8_CHECKSUM = "5986ee1ea743a34bf42cb49d3a7d5ded4d0b884bb653cb090e3d4b7f679a4552"
# The v8 schema before the framework rename (one comment differs); databases made then have it.
V8_PRE_RENAME_CHECKSUM = "315af82775eb32324307b33c7f88b18a7fd1a9b443ee1b8cb15d42fa7b1945ca"
V9_CHECKSUM = "bf4c540376649ecf841e5207e2581cda29cba76473761b7e5c27e72cee98b0ee"
V10_CHECKSUM = "9e993488d6062b3a9082bdab5da44d5f1cf20ed849be641a6c6fd41a9ce9b9f9"
V11_CHECKSUM = "e35fda97a06d674a160f277f0807408cbd7435f4eca571ab70773dd20715452c"
MIGRATABLE = {(4, V4_CHECKSUM): 4, (5, V5_CHECKSUM): 5, (6, V6_CHECKSUM): 6, (7, V7_CHECKSUM): 7}
MIGRATABLE |= {(8, V8_CHECKSUM): 8, (8, V8_PRE_RENAME_CHECKSUM): 8, (9, V9_CHECKSUM): 9}
MIGRATABLE |= {(10, V10_CHECKSUM): 10, (11, V11_CHECKSUM): 11}
PERFORMANCE = ("concurrency", "timeout_seconds", "requests_per_second")


def _schema():
    schema = Path(__file__).with_name("schema.sql").read_text()
    return schema, hashlib.sha256(schema.encode()).hexdigest()


def initialize(credentials_file):
    schema, checksum = _schema()
    with connect(credentials_file) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(817452901)")
        if conn.execute("SELECT to_regclass(current_schema||'.schema_versions')").fetchone()[0]:
            versions = conn.execute("SELECT version,checksum FROM schema_versions").fetchall()
            if versions != [(SCHEMA_VERSION, checksum)]:
                raise ValueError(
                    "Schema differs; run db-migrate (v4 to v7) or rebuild into a new database"
                )
        conn.execute(schema)
        conn.execute(
            "INSERT INTO schema_versions(version,checksum) VALUES (%s,%s) ON CONFLICT DO NOTHING",
            (SCHEMA_VERSION, checksum),
        )
    return {"schema_version": SCHEMA_VERSION}


def _add_run_metrics(metrics, summary, attempted, new_ips, histogram):
    """Add one run to a scope's cached metrics; histogram is the merged lifetime one."""
    for name in (
        "requested",
        "started",
        "completed",
        "successes",
        "total_errors",
        "cancelled",
        "not_started",
        "response_body_bytes",
        "duration_seconds",
    ):
        value = attempted if name == "requested" else summary[name]
        metrics[name] = metrics.get(name, 0) + value
    metrics["runs"] = metrics.get("runs", 0) + 1
    metrics["unique_ips"] = (
        metrics.get("unique_ips", 0) + new_ips if summary["pool_eligible"] else None
    )
    for name in ("errors", "status_codes"):
        counts = Counter(metrics.get(name, {}))
        counts.update(summary[name])
        metrics[name] = dict(counts)
    metrics["latency_ms"] = histogram.percentiles()
    metrics["latency_mean_ms"] = histogram.mean()
    metrics["success_rate"] = (
        metrics["successes"] / metrics["completed"] if metrics["completed"] else None
    )


def _lock(conn, action):
    """Take the ingestion lock and require the current schema before the action."""
    conn.execute("SELECT pg_advisory_xact_lock(817452901)")
    if conn.execute("SELECT max(version) FROM schema_versions").fetchone()[0] != SCHEMA_VERSION:
        raise ValueError(f"Database schema requires db-migrate or a rebuild before {action}")


def _upsert_product(conn, provider, product):
    """Add the provider and product where new; return the product_id."""
    conn.execute("INSERT INTO providers VALUES (%s) ON CONFLICT DO NOTHING", (provider,))
    conn.execute(
        "INSERT INTO products(provider,name) VALUES (%s,%s) ON CONFLICT DO NOTHING",
        (provider, product),
    )
    return conn.execute(
        "SELECT product_id FROM products WHERE provider=%s AND name=%s", (provider, product)
    ).fetchone()[0]


def _upsert_pool(conn, run, product_id):
    """Add the run's target and pool where new; return the pool_id."""
    summary, scope = run.summary, run.scope
    pool_definition = {
        "target": scope["target"],
        "config": {k: v for k, v in scope["config"].items() if k not in PERFORMANCE},
    }
    pool_hash = hashlib.sha256(json.dumps(pool_definition, sort_keys=True).encode()).hexdigest()
    profile = _profile(pool_definition, run.provider, run.product)
    # Pools and scopes reference targets; a target nobody seeded joins unscheduled.
    target = summary["target"]
    conn.execute(
        "INSERT INTO targets(target_id,url,engine,census,pulse_enabled) "
        "VALUES (%s,%s,%s,false,false) ON CONFLICT DO NOTHING",
        (
            profile["target_id"],
            target["url"] if type(target.get("url")) is str else "",
            target["engine"] if type(target.get("engine")) is str else "http",
        ),
    )
    conn.execute(
        "INSERT INTO pool_stats(product_id,pool_hash,"
        + ",".join(profile)
        + ") VALUES ("
        + ",".join(["%s"] * (len(profile) + 2))
        + ") ON CONFLICT DO NOTHING",
        (product_id, pool_hash, *profile.values()),
    )
    return conn.execute(
        "SELECT pool_id FROM pool_stats WHERE product_id=%s AND pool_hash=%s",
        (product_id, pool_hash),
    ).fetchone()[0]


def _upsert_scope(conn, run, performance):
    """Add the run's provider, product, target, pool and scope where new.

    Returns product_id, pool_id and the scope's (scope_id, metrics, histogram_steps,
    histogram_counts).
    """
    product_id = _upsert_product(conn, run.provider, run.product)
    pool_id = _upsert_pool(conn, run, product_id)
    scope_key = (product_id, run.summary["target"]["id"], run.scope_hash)
    conn.execute(
        "INSERT INTO scope_stats(product_id,target,scope_hash,pool_id,"
        + ",".join(PERFORMANCE)
        + ") VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
        (*scope_key, pool_id, *performance),
    )
    scope_row = conn.execute(
        "SELECT scope_id,metrics,histogram_steps,histogram_counts FROM scope_stats WHERE "
        "product_id=%s AND target=%s AND scope_hash=%s",
        scope_key,
    ).fetchone()
    return product_id, pool_id, scope_row


def _performance(scope):
    """The scope's concurrency, timeout and rate columns; concurrency and timeout are required."""
    try:
        performance = [
            column(kind, scope["config"][key])
            for key, kind in zip(PERFORMANCE, ("integer", "number", "number"), strict=True)
        ]
        if None in performance[:2]:
            raise TypeError
    except (KeyError, TypeError):
        raise ValueError("Unsupported concurrency, timeout or rate setting") from None
    return performance


def _existing(conn, run, batch_id):
    """The run's stored (summary_sha256, run_key, batch_id), linked to batch_id; None if new."""
    existing = conn.execute(
        "SELECT summary_sha256,run_key,batch_id FROM test_runs WHERE run_id=%s", (run.run_id,)
    ).fetchone()
    if existing and existing[0] != run.summary_hash:
        raise ValueError("Run ID already exists with different content")
    if existing and batch_id is not None and existing[2] != batch_id:
        if existing[2] is not None:
            raise ValueError("Run already belongs to another batch")
        conn.execute("UPDATE test_runs SET batch_id=%s WHERE run_key=%s", (batch_id, existing[1]))
    return existing


def _store_run(conn, run, performance, batch_id):
    """Store a new run with its buckets, geo rows, addresses and scope totals; return run_key."""
    histogram = run.histogram
    product_id, pool_id, scope_row = _upsert_scope(conn, run, performance)
    scope_id, metrics, histogram_steps, histogram_counts = scope_row
    run_key = storage_ingest.insert_run(conn, run, scope_id, batch_id)
    storage_ingest.insert_buckets(conn, run, run_key)
    storage_ingest.store_geo(conn, run_key, run.geo_rows)
    storage_ingest.check_stored(conn, run, run_key)
    new_ips = _add_addresses(conn, run.provider, product_id, scope_id)
    storage_ingest.add_observations(conn, run_key, pool_id)
    lifetime = zip(accumulate(histogram_steps), histogram_counts, strict=True)
    histogram.merge(Histogram(Counter(dict(lifetime))))
    _add_run_metrics(metrics, run.summary, run.attempted, new_ips, histogram)
    conn.execute(
        "UPDATE scope_stats SET metrics=%s,histogram_steps=%s,histogram_counts=%s,"
        "updated_at=now() WHERE scope_id=%s",
        (Jsonb(metrics), *_steps(histogram.export()["bins"]), scope_id),
    )
    return run_key


def ingest(directory, credentials_file, *, batch_id=None, ip_rows=True):
    """Import one finished run directory; replaying the same artifacts is a no-op.

    batch_id links the run to its test_batches row; ip_rows=False (pulse) stores no per-IP rows.
    A census may have requested=null (unbounded); geo.jsonl.gz, if listed, fills run_geo_buckets.
    """
    directory = Path(directory)
    run = _read_run(directory)
    run_id = run.run_id
    performance = _performance(run.scope)
    with connect(credentials_file) as conn:
        # Serialize ingestion; measurements and website reads run independently.
        # The entire history/enrichment/cache update commits together.
        _lock(conn, "import")
        existing = _existing(conn, run, batch_id)
        rows = storage_ingest.load_ips(conn, run, ip_rows)
        run_key = existing[1] if existing else _store_run(conn, run, performance, batch_id)
        enriched = storage_ingest.enrich(conn, directory, run_id, run_key, rows) if ip_rows else 0
    return {
        "run_id": run_id,
        "run_key": run_key,
        "imported": not bool(existing),
        "observation_rows": rows,
        "enrichment_releases_added": enriched,
        "scope_hash": run.scope_hash,
    }


def _add_addresses(conn, provider, product_id, scope_id):
    """Add the new addresses and product memberships; return the IPs new to this scope."""
    conn.execute(
        "INSERT INTO ip_addresses(ip) SELECT DISTINCT ip FROM incoming_ips "
        "ON CONFLICT(ip) DO NOTHING"
    )
    _membership(conn, provider, product_id)
    # Avoid another persisted membership set per performance configuration.
    # First presence within this exact scope: the anti-join reads via test_runs_scope.
    return conn.execute(
        "SELECT count(*) FROM (SELECT DISTINCT a.ip_id FROM incoming_ips i "
        "JOIN ip_addresses a USING(ip)) n WHERE NOT EXISTS (SELECT 1 FROM "
        "ip_observations o JOIN test_runs r USING(run_key) "
        "WHERE o.ip_id=n.ip_id AND r.scope_id=%s)",
        (scope_id,),
    ).fetchone()[0]


def _membership(conn, provider, product_id):
    # Only first-ever presence at this provider contributes to lifetime overlap.
    conn.execute(
        "CREATE TEMP TABLE new_provider_ips ON COMMIT DROP AS "
        "SELECT DISTINCT a.ip_id FROM incoming_ips i JOIN ip_addresses a USING(ip) "
        "WHERE NOT EXISTS (SELECT 1 FROM product_ips m JOIN products p USING(product_id) "
        "WHERE m.ip_id=a.ip_id AND p.provider=%s)",
        (provider,),
    )
    peers = conn.execute(
        "SELECT p.provider,count(DISTINCT n.ip_id) FROM new_provider_ips n "
        "JOIN product_ips m USING(ip_id) JOIN products p USING(product_id) "
        "WHERE p.provider<>%s GROUP BY p.provider",
        (provider,),
    ).fetchall()
    for peer, count in peers:
        a, b = sorted((provider, peer))
        conn.execute(
            "INSERT INTO provider_overlap(provider_a,provider_b,shared_ips) "
            "VALUES (%s,%s,%s) ON CONFLICT(provider_a,provider_b) DO UPDATE SET "
            "shared_ips=provider_overlap.shared_ips+excluded.shared_ips,updated_at=now()",
            (a, b, count),
        )
    conn.execute(
        "INSERT INTO product_ips SELECT %s,a.ip_id,min(i.first_seen),max(i.last_seen),"
        "sum(i.count),1 FROM incoming_ips i JOIN ip_addresses a USING(ip) GROUP BY a.ip_id "
        "ON CONFLICT(product_id,ip_id) DO UPDATE SET "
        "first_seen=least(product_ips.first_seen,excluded.first_seen),"
        "last_seen=greatest(product_ips.last_seen,excluded.last_seen),"
        "observation_count=product_ips.observation_count+excluded.observation_count,"
        "run_count=product_ips.run_count+1",
        (product_id,),
    )


def _migration_start(versions, checksum, layer_stale):
    """The base schema version to migrate from; None when base and calculated layer are current."""
    if versions == [(SCHEMA_VERSION, checksum)]:
        return SCHEMA_VERSION if layer_stale else None
    return storage_migrate.migratable_start(versions, MIGRATABLE)


def _compact(credentials_file, tables):
    """VACUUM FULL the rebuilt tables, after and outside the migration's transaction."""
    if tables:
        with connect(credentials_file) as conn:
            conn.autocommit = True
            conn.execute("VACUUM (FULL, ANALYZE) " + ",".join(tables))


def migrate(credentials_file):
    """Migrate an unmodified v4 to v11 database, and its calculated layer, in one transaction.

    v4 runs migrate_v4_v5.sql (and calculated v1 → v2), then migrate_v5_v6.sql to v11_v12.sql in
    turn; v5 to v11 start at the step for their version (v7 and v9 only add a table, v8 moves
    names into the shared lists, v10 adds the batch kind browser, v11 swaps an index). A
    calculated v2 layer then runs calculated/migrate_v2_v3.sql, then calculated v3 → v4
    (availability), v4 → v5 (city/network evidence), v5 → v6 (breakdowns partitioned by build),
    v6 → v7 (names), v7 → v8 (parts), v8 → v9 (lists), v9 → v10 (memo tables), v10 → v11 and
    v11 → v12 (rollups) and v12 → v13 (list rows without their foreign key); each is verified.
    A v6 or later database with an older calculated layer starts at its step. Every rebuilt pool,
    scope, bucket and run summary, every test batch, location, network and per-IP enrichment, and
    every calculated window, breakdown and score, must equal its original.
    """
    from .calculated import store

    schema, checksum = _schema()
    calculated, calculated_checksum = store.schema()
    with connect(credentials_file) as conn:
        versions, layered, layers = storage_migrate.read_versions(conn)
        current_layer = [(store.SCHEMA_VERSION, calculated_checksum)]
        start = _migration_start(versions, checksum, layered and layers != current_layer)
        if start is None:
            return {"schema_version": SCHEMA_VERSION, "migrated": False}
        upgraded, later = storage_migrate.layer_start(start, layered, layers, current_layer)
        stale = layered and not upgraded
        checks = storage_migrate.migrate_base(
            conn, start, layered, stale and not later, SCHEMA_VERSION
        )
        steps = storage_migrate.migrate_calculated(conn, start, stale, later)
        conn.execute(schema)
        if layered:
            conn.execute(calculated)
        verified = [*storage_migrate.verify(conn, checks, start), *steps]
        storage_migrate.record(
            conn,
            (SCHEMA_VERSION, checksum) if start < SCHEMA_VERSION else None,
            (store.SCHEMA_VERSION, calculated_checksum) if stale else None,
        )
    _compact(credentials_file, storage_migrate.compacted(start, layered, upgraded))
    return {
        "schema_version": SCHEMA_VERSION,
        "from_schema_version": start,
        "calculated_schema_version": store.SCHEMA_VERSION if layered else None,
        "from_calculated_schema_version": layers[0][0] if layered else None,
        "migrated": True,
        "verified": verified,
    }
