"""Transactional, replay-safe PostgreSQL ingestion of durable measurement artifacts."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from itertools import accumulate
from pathlib import Path

import psycopg as psycopg  # re-exported: scripts catch storage.psycopg errors
from psycopg.types.json import Jsonb

from . import storage_batch, storage_ingest, storage_migrate
from .partitions import RAW_DAYS as RAW_DAYS  # re-exported for the calculator
from .partitions import creation, holder, monthly
from .partitions import partition as partition
from .stats import Histogram
from .storage_batch import POOL
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
from .storage_seed import seed_targets as seed_targets
from .storage_status import status as status

SCHEMA_VERSION = 15
# Only an unmodified v4 to v14 schema (these checksums) is migrated in place; migrate().
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
V12_CHECKSUM = "f7c2e9be2e168ac30d0542a71ed1c243a398b0436c36a4e790c3ee8d38d6b7e2"
V13_CHECKSUM = "1a654b6ad447dccaf5f3ab03e0a96383d879e547ebcf4b10b24e98f74a5af1f9"
V14_CHECKSUM = "85f979d4205a96f10bc85f8a9c83c9fb3510329dcbc499b9ca9de79c6fb1ba53"
MIGRATABLE = {(4, V4_CHECKSUM): 4, (5, V5_CHECKSUM): 5, (6, V6_CHECKSUM): 6, (7, V7_CHECKSUM): 7}
MIGRATABLE |= {(8, V8_CHECKSUM): 8, (8, V8_PRE_RENAME_CHECKSUM): 8, (9, V9_CHECKSUM): 9}
MIGRATABLE |= {(10, V10_CHECKSUM): 10, (11, V11_CHECKSUM): 11, (12, V12_CHECKSUM): 12}
MIGRATABLE |= {(13, V13_CHECKSUM): 13, (14, V14_CHECKSUM): 14}
PERFORMANCE = ("concurrency", "timeout_seconds", "requests_per_second")
# Imports hold the ingest lock shared, so they run side by side (INGEST-PARALLEL-1) while a purge,
# seed, migration or rebuild, which holds it exclusively, waits for them and they for it. Imports
# of one network go one at a time: each takes its networks' locks before any write (so its
# network's counts, _network, see every earlier import of it); only the cross-network overlap
# (_overlap), the last statement, takes the overlap lock.
# 817452902 is the calculation's lock and 817452903 detect's (detect.LOCK).
INGEST_LOCK, OVERLAP_LOCK = 817452901, 817452905
# Locks of two integers (never equal to a one-number key), taken at the start in this order,
# each set sorted: the batch's networks' (providers'), then its runs' (two imports of one run go
# one after the other).
RUN_LOCK, NETWORK_LOCK = 817452904, 817452906
NETWORK_LOCKS = (
    f"SELECT pg_advisory_xact_lock({NETWORK_LOCK},h) FROM (SELECT DISTINCT hashtext(p) h "
    "FROM unnest(%s::text[]) p ORDER BY h) s"
)
RUN_LOCKS = (
    f"SELECT pg_advisory_xact_lock({RUN_LOCK},h) FROM (SELECT DISTINCT hashtext(r::text) h "
    "FROM unnest(%s::uuid[]) r ORDER BY h) s"
)


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


class IngestBatchError(Exception):
    """A batch rolled back (its cause stopped it); retry its runs (run_ids) one at a time."""

    def __init__(self, run_ids):
        super().__init__("Ingest batch rolled back: " + ", ".join(run_ids))
        self.run_ids = run_ids


def _lock(conn, action):
    """Take the ingestion lock and require the current schema before the action."""
    conn.execute("SELECT pg_advisory_xact_lock(817452901)")
    if conn.execute("SELECT max(version) FROM schema_versions").fetchone()[0] != SCHEMA_VERSION:
        raise ValueError(f"Database schema requires db-migrate or a rebuild before {action}")


def _pool(run):
    """The run's target url and engine (for a target nobody seeded), pool hash and pool columns."""
    summary, scope = run.summary, run.scope
    pool_definition = {
        "target": scope["target"],
        "config": {k: v for k, v in scope["config"].items() if k not in PERFORMANCE},
    }
    pool_hash = hashlib.sha256(json.dumps(pool_definition, sort_keys=True).encode()).hexdigest()
    profile = _profile(pool_definition, run.provider, run.product)
    target = summary["target"]
    return (
        target["url"] if type(target.get("url")) is str else "",
        target["engine"] if type(target.get("engine")) is str else "http",
        pool_hash,
        *(profile[name] for name in POOL),
    )


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


def _rows(runs):
    """The temp tables' rows: new runs with their buckets and IPs, every run's reports to add."""
    rows = {table: [] for table in storage_batch.TABLES}
    for idx, run in enumerate(runs):
        for release, metadata, checksum, enriched in run.added:
            rows["incoming_releases"].append((idx, release, metadata, checksum, run.run_key))
            rows["incoming_enrichment"] += [(idx, release, *row) for row in enriched]
        if run.stored:
            continue
        run_row = storage_ingest.run_row(run, run.batch_id)
        named = (run.provider, run.product, run.scope_hash, *run.performance)
        rows["incoming_runs"].append((idx, *run_row, *named, *_pool(run)))
        rows["incoming_buckets"] += [(idx, b, *values) for b, values in run.bucket_rows.items()]
        rows["incoming_ips"] += [(idx, *row) for row in run.ips]
    return rows


def _store_runs(conn, runs, fresh, existing):
    """Queue the batch's statements after the first round trip, its runs' own rows first, then
    each network's counts; the cursors to read after it."""
    storage_batch.load(conn, _rows(runs))
    # A stored run without a batch joins the one it is imported with.
    unlinked = [r for r in runs if r.stored and r.stored.get("batch_id") is None]
    links = [(r.run_key, r.batch_id) for r in unlinked if r.batch_id is not None]
    storage_batch.link(conn, links)
    storage_batch.upsert_scopes(conn)
    storage_ingest.insert_runs(conn)
    storage_ingest.insert_buckets(conn, fresh, existing)
    storage_ingest.store_geo(conn, fresh)
    addresses = any(run.ips for run in fresh)
    if addresses:
        _add_addresses(conn)
    enriched = storage_ingest.enrich(conn) if any(run.added for run in runs) else None
    summaries = conn.execute(storage_batch.SUMMARIES)
    new_ips = _network(conn) if addresses else None
    scopes = conn.execute(storage_batch.SCOPES)
    return summaries, scopes, new_ips, enriched


def _scope_totals(runs, scopes, new_ips):
    """(scope_id, metrics, histogram_steps, histogram_counts) after the new runs, in batch order."""
    totals = {}
    for idx, scope_id, metrics, histogram_steps, histogram_counts in scopes:
        run, histogram = runs[idx], runs[idx].histogram
        metrics, steps, counts = totals.get(scope_id, (metrics, histogram_steps, histogram_counts))
        lifetime = zip(accumulate(steps), counts, strict=True)
        histogram.merge(Histogram(Counter(dict(lifetime))))
        _add_run_metrics(metrics, run.summary, run.attempted, new_ips.get(idx, 0), histogram)
        totals[scope_id] = (metrics, *_steps(histogram.export()["bins"]))
    return [(scope_id, Jsonb(m), *bins) for scope_id, (m, *bins) in totals.items()]


def _read(idx, directory, batch_id, ip_rows):
    """Read and check a run directory with its IP rows and enrichment reports: no file I/O later."""
    run = _read_run(Path(directory))
    run.performance = _performance(run.scope)
    run.ips = storage_ingest.read_ips(run, ip_rows)
    run.reports = storage_ingest.read_enrichments(run) if ip_rows else []
    run.idx, run.batch_id = idx, batch_id
    return run


def _begin(conn, pipeline, runs):
    """Open the import's transaction under its locks, taken before any write in one order: the
    ingest lock shared, its networks', its runs'. The stored runs (STORED rows) and the names of
    the partitions found, after one round trip; every read after the locks (READ COMMITTED)
    sees every import of the networks committed before."""
    conn.execute("BEGIN")
    conn.execute(f"SELECT pg_advisory_xact_lock_shared({INGEST_LOCK})")
    conn.execute(NETWORK_LOCKS, (sorted({run.provider for run in runs}),))
    conn.execute(RUN_LOCKS, ([run.run_id for run in runs],))
    version = conn.execute("SELECT max(version) FROM schema_versions")
    stored = conn.execute(storage_batch.STORED, ([run.run_id for run in runs],))
    # The names partition() looks for: the month's partition, then the day's.
    wanted = storage_ingest.partitions(runs)
    names = sorted({n for t, m in wanted for n in (monthly(t, m)[0], holder(t, m, ())[0])})
    found = conn.execute(
        "SELECT n FROM unnest(%s::text[]) n WHERE to_regclass(n) IS NOT NULL", (names,)
    )
    pipeline.sync()
    if version.fetchone()[0] != SCHEMA_VERSION:
        raise ValueError("Database schema requires db-migrate or a rebuild before import")
    return stored.fetchall(), {n for (n,) in found}


def _create_partitions(conn, missing):
    """Leave the import's transaction and queue the missing partitions' creation in one of their
    own, under the ingest lock held exclusively: creating one locks its parent and the tables its
    foreign keys reference, so no import may hold locks on them meanwhile (it would deadlock)."""
    conn.execute("ROLLBACK")
    conn.execute("BEGIN")
    conn.execute(f"SELECT pg_advisory_xact_lock({INGEST_LOCK})")
    for table, name, start, end in missing:
        for query in creation(table, name, start, end):
            conn.execute(query)
    conn.execute("COMMIT")


def _store(runs, conn):
    """Store read runs in one transaction and three round trips on conn: the shared lock with the
    reads under it, every statement of the batch, the scope totals and the provider overlap
    (under the overlap lock) with COMMIT. A batch that
    needs a new day's partitions creates them and begins again (one more round trip)."""
    conn.autocommit = True  # BEGIN below: psycopg's own would take a round trip in pipeline mode
    with conn.pipeline() as pipeline:
        # The entire history/enrichment/cache update commits together.
        stored, existing = _begin(conn, pipeline, runs)
        missing = storage_ingest.missing(storage_batch.existing(runs, stored), existing)
        if missing:
            _create_partitions(conn, missing)  # sent with the next begin
            stored, existing = _begin(conn, pipeline, runs)
        fresh = storage_batch.existing(runs, stored)
        summaries, scopes, new_ips, enriched = _store_runs(conn, runs, fresh, existing)
        pipeline.sync()
        for idx, run_key, summary in summaries.fetchall():
            storage_ingest.check_stored(runs[idx], summary)
            runs[idx].run_key = run_key
        if enriched and enriched[0].fetchall():
            raise ValueError("Conflicting enrichment for the same IP")
        if enriched and enriched[1].fetchone()[0]:
            raise ValueError("Enrichment release conflicts with prior data")
        addresses, new_ips = new_ips is not None, dict(new_ips.fetchall()) if new_ips else {}
        storage_batch.update_scopes(conn, _scope_totals(runs, scopes.fetchall(), new_ips))
        if addresses:
            _overlap(conn)
        conn.execute("COMMIT")  # sent with the scope totals; the pipeline's end waits for all
    return [
        {"run_id": run.run_id, "run_key": run.run_key, "imported": not run.stored}
        | {"observation_rows": len(run.ips), "enrichment_releases_added": len(run.added)}
        | {"scope_hash": run.scope_hash}
        for run in runs
    ]


def _read_all(directories, batch_ids, ip_rows):
    directories = list(directories)
    batch_ids = [None] * len(directories) if batch_ids is None else list(batch_ids)
    if len(batch_ids) != len(directories):
        raise ValueError("Give one batch id per run directory")
    pairs = enumerate(zip(directories, batch_ids, strict=True))
    return [_read(n, directory, batch_id, ip_rows) for n, (directory, batch_id) in pairs]


def _connected(runs, credentials_file):
    with connect(credentials_file) as conn:
        return _store(runs, conn)


def ingest_many(directories, credentials_file, *, batch_ids=None, ip_rows=True, conn=None):
    """Import run directories in one transaction and a fixed number of round trips, storing what
    importing them one at a time in this order does; per directory, what ingest returns.

    batch_ids holds one batch id (or None) per directory. Each directory is checked first and
    raises its own error; any error in the transaction rolls the batch back and raises
    IngestBatchError naming its run ids, to retry one at a time with ingest. With conn (an open
    connection, idle) the batch goes over it and credentials_file is not read.
    """
    runs = _read_all(directories, batch_ids, ip_rows)
    try:
        return _store(runs, conn) if conn else _connected(runs, credentials_file)
    except (psycopg.Error, ValueError) as error:
        raise IngestBatchError([run.run_id for run in runs]) from error


def ingest(directory, credentials_file, *, batch_id=None, ip_rows=True):
    """Import one finished run directory; replaying the same artifacts is a no-op.

    batch_id links the run to its test_batches row; ip_rows=False (pulse) stores no per-IP rows.
    A census may have requested=null (unbounded); geo.jsonl.gz, if listed, fills run_geo_buckets.
    It is ingest_many of one directory, raising the run's own error, not IngestBatchError.
    """
    return _connected(_read_all([directory], [batch_id], ip_rows), credentials_file)[0]


def _add_addresses(conn):
    """Add the new addresses and observations: the runs' own rows."""
    # Every insert into a table other imports share goes in key order: no two wait on each other.
    conn.execute(
        "INSERT INTO ip_addresses(ip) SELECT DISTINCT ip FROM incoming_ips ORDER BY ip "
        "ON CONFLICT(ip) DO NOTHING"
    )
    conn.execute(
        "CREATE TEMP TABLE incoming_observations ON COMMIT DROP AS SELECT i.idx,i.bucket,a.ip_id,"
        "i.count,i.first_seen,i.last_seen,r.run_key,r.provider,r.product_id,r.pool_id,r.scope_id "
        "FROM incoming_ips i JOIN incoming_runs r USING(idx) JOIN ip_addresses a USING(ip)"
    )
    conn.execute("ANALYZE incoming_observations")
    storage_ingest.add_observations(conn)


def _network(conn):
    """The network's counts (under its lock): the IPs new to each pool, to each provider (before
    its product memberships are added) and to each scope; the cursor of (idx, IPs new to its
    scope)."""
    storage_ingest.add_pool_ips(conn)
    # Only first-ever presence at this provider contributes to lifetime overlap (_overlap).
    conn.execute(
        "CREATE TEMP TABLE new_provider_ips ON COMMIT DROP AS "
        "SELECT DISTINCT n.provider,n.ip_id FROM incoming_observations n "
        "WHERE NOT EXISTS (SELECT 1 FROM product_ips m JOIN products p USING(product_id) "
        "WHERE m.ip_id=n.ip_id AND p.provider=n.provider)"
    )
    conn.execute(
        "INSERT INTO product_ips SELECT product_id,ip_id,min(first_seen),max(last_seen),"
        "sum(count),count(DISTINCT idx) FROM incoming_observations GROUP BY product_id,ip_id "
        "ORDER BY product_id,ip_id ON CONFLICT(product_id,ip_id) DO UPDATE SET "
        "first_seen=least(product_ips.first_seen,excluded.first_seen),"
        "last_seen=greatest(product_ips.last_seen,excluded.last_seen),"
        "observation_count=product_ips.observation_count+excluded.observation_count,"
        "run_count=product_ips.run_count+excluded.run_count"
    )
    return storage_ingest.add_scope_ips(conn)


def _overlap(conn):
    """The cross-network step, the last before COMMIT: each IP new to its provider counts once
    per other provider with it, stored (product_ips, read after the overlap lock, so every
    import committed before is seen) or in the batch; pairs in codepoint order."""
    conn.execute(f"SELECT pg_advisory_xact_lock({OVERLAP_LOCK})")
    pair = 'n.provider COLLATE "C",o.provider COLLATE "C"'
    conn.execute(
        f"INSERT INTO provider_overlap(provider_a,provider_b,shared_ips) SELECT least({pair}),"
        f"greatest({pair}),count(DISTINCT n.ip_id) FROM new_provider_ips n JOIN (SELECT "
        "p.provider,m.ip_id FROM (SELECT DISTINCT ip_id FROM new_provider_ips) x JOIN product_ips "
        "m USING(ip_id) JOIN products p USING(product_id) UNION ALL SELECT provider,ip_id FROM "
        "new_provider_ips) o ON o.ip_id=n.ip_id AND o.provider<>n.provider GROUP BY 1,2 "
        "ORDER BY 1,2 ON CONFLICT(provider_a,provider_b) DO UPDATE SET "
        "shared_ips=provider_overlap.shared_ips+excluded.shared_ips,updated_at=now()"
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
    """Migrate an unmodified v4 to v14 database, and its calculated layer, in one transaction.

    v4 runs migrate_v4_v5.sql (and calculated v1 → v2), then migrate_v5_v6.sql to v14_v15.sql in
    turn; v5 to v14 start at the step for their version (v7 and v9 only add a table, v8 moves
    names into the shared lists, v10 adds the batch kind browser, v11 swaps an index, v12 adds
    the live tape's bucket counts, filled from the stored events, and their trigger; v13 adds
    scope_ips, filled from the stored observations; v14 adds three partial test_events indexes). A
    calculated v2 layer then runs calculated/migrate_v2_v3.sql, then calculated v3 → v4
    (availability), v4 → v5 (city/network evidence), v5 → v6 (breakdowns partitioned by build),
    v6 → v7 (names), v7 → v8 (parts), v8 → v9 (lists), v9 → v10 (memo tables), v10 → v11 and
    v11 → v12 (rollups), v12 → v13 (list rows without their foreign key) and v13 → v14 (the
    API head); each is verified.
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
