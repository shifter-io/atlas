"""In-place v4 → … → v12, v5 → … → v12, v6 → … → v12, v7 → … → v12, v8 → … → v12 and v9 → … → v12
migrations against a pg_dump of a v4 database.

fixtures/database_v4.sql.gz was produced by proxy-index 0.8.0 (schema v4, calculated v1): two
benchmarked providers with enrichment and evidence, a third with a Shifter-style provider_config,
float timeout, rate, custom URL, an unknown config key and a non-canonical bucket label; three
builds (ready and ranked, then expired and retained scores, then an unpublished draft).
fixtures/schema_v5.sql is the exact v5 schema (storage.V5_CHECKSUM) and fixtures/calculated_v2.sql
the exact calculated v2 schema (store.V2_CHECKSUM); load_v5 builds a v5 database from the same dump
the way the v5 release migrated it; fixtures/schema_v6.sql and schema_v7.sql are the v6 and v7
schemas except for one SQL comment each that the rename (#3) changed, so their own checksums are
V6_FIXTURE_SHA256 and V7_FIXTURE_SHA256, while the loaders record storage.V6_CHECKSUM and
V7_CHECKSUM as a real v6 or v7 database does; fixtures/schema_v8.sql is the exact v8 schema
(storage.V8_CHECKSUM), and fixtures/calculated_v6.sql the exact calculated v6 schema
(store.V6_CHECKSUM); fixtures/schema_v9.sql is the exact v9 schema (storage.V9_CHECKSUM). Every
path ends at schema v12 with calculated v12; test_migration_v11.py and _v12.py start at v10, v11.
"""

import gzip
import hashlib
import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_calculated import payload_mismatches
from test_storage import measurement

from proxy_index import names
from proxy_index.calculated import store
from proxy_index.calculated.api import app
from proxy_index.calculated.calculator import calculate
from proxy_index.storage import (
    SCHEMA_VERSION,
    V5_CHECKSUM,
    V6_CHECKS,
    V6_CHECKSUM,
    V7_CHECKSUM,
    V8_CHECKSUM,
    V8_PRE_RENAME_CHECKSUM,
    V9_CHECKSUM,
    connect,
    ingest,
    initialize,
    migrate,
    read_summary,
    seed,
    status,
)

pytest_plugins = ["test_storage"]

FIXTURE = Path(__file__).parent / "fixtures/database_v4.sql.gz"
SCHEMA_V5 = Path(__file__).parent / "fixtures/schema_v5.sql"
SCHEMA_V6 = Path(__file__).parent / "fixtures/schema_v6.sql"
SCHEMA_V7 = Path(__file__).parent / "fixtures/schema_v7.sql"
SCHEMA_V8 = Path(__file__).parent / "fixtures/schema_v8.sql"
SCHEMA_V9 = Path(__file__).parent / "fixtures/schema_v9.sql"
# The fixtures' own SHA-256: the shipped v6 and v7 schemas except for one SQL comment that the
# rename (#3) changed. A real v6 or v7 database records storage.V6_CHECKSUM or V7_CHECKSUM, which
# the loaders below still insert into schema_versions.
V6_FIXTURE_SHA256 = "6af11b950a40157784afffcb428f4f2d9d9ce7ac9d34562644383d6150f40d02"
V7_FIXTURE_SHA256 = "4968de7d7b7bbae34518df9653e1f225ee6b9e6aa37c9c5f00ba9fd997ed0887"
CALCULATED_V2 = Path(__file__).parent / "fixtures/calculated_v2.sql"
CALCULATED_V3 = Path(__file__).parent / "fixtures/calculated_v3.sql"
CALCULATED_V4 = Path(__file__).parent / "fixtures/calculated_v4.sql"
CALCULATED_V5 = Path(__file__).parent / "fixtures/calculated_v5.sql"
CALCULATED_V6 = Path(__file__).parent / "fixtures/calculated_v6.sql"
SOURCE = Path(store.__file__).parents[1]
POLICY = Path("configs/methodology.toml")
RESOURCES = ("products", "scores", "series", "breakdowns", "overlap", "fleet", "runs")


def load_v4(database):
    with connect(database) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    with connect(database) as conn:
        conn.execute(gzip.decompress(FIXTURE.read_bytes()).decode())


def load_v5(database, *, layered=True):
    """The v4 dump after the v5 release's migration: v4 → v5 SQL, then the v5 schema."""
    load_v4(database)
    schema = SCHEMA_V5.read_text()
    calculated = CALCULATED_V2.read_text()
    assert hashlib.sha256(schema.encode()).hexdigest() == V5_CHECKSUM
    assert hashlib.sha256(calculated.encode()).hexdigest() == store.V2_CHECKSUM
    with connect(database) as conn:
        conn.execute((SOURCE / "migrate_v4_v5.sql").read_text())
        conn.execute((SOURCE / "calculated/migrate_v1_v2.sql").read_text())
        conn.execute(schema)
        conn.execute(calculated)
        conn.execute("DELETE FROM schema_versions")
        conn.execute("INSERT INTO schema_versions(version,checksum) VALUES (5,%s)", (V5_CHECKSUM,))
        conn.execute("DELETE FROM calculation_schema")
        conn.execute("INSERT INTO calculation_schema VALUES (2,%s)", (store.V2_CHECKSUM,))


def canonical(value):
    return json.dumps(value, sort_keys=True, default=str)


def v4_state(database):
    with connect(database) as conn:
        return {
            "summaries": dict(
                conn.execute("SELECT run_id::text,stored_run_summary(run_key) FROM test_runs")
            ),
            "scopes": dict(
                conn.execute("SELECT scope_hash,jsonb_build_array(scope,metrics) FROM scope_stats")
            ),
            "pools": dict(conn.execute("SELECT pool_hash,definition FROM pool_stats")),
            "payloads": dict(conn.execute("SELECT build_id,payload FROM calculated_snapshots")),
            "evidence": conn.execute("SELECT * FROM metric_evidence ORDER BY 1").fetchall(),
        }


async def api_bodies(database, builds):
    bodies = {}
    async with TestClient(TestServer(app(database))) as client:
        paths = [f"/v1/snapshots/{b}" for b in builds] + [
            f"/v1/snapshots/{b}/{r}?window={w}"
            for b in builds
            for r in RESOURCES
            for w in ("24h", "7d", "30d")
        ]
        for path in paths:
            response = await client.get(path)
            bodies[path] = (response.status, response.headers["ETag"], await response.read())
    return bodies


def catalog(database):
    """Columns, constraints, indexes, views and functions of the non-partition objects."""
    queries = [
        "SELECT c.relname,a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull,"
        "pg_get_expr(d.adbin,d.adrelid),a.attidentity FROM pg_attribute a "
        "JOIN pg_class c ON c.oid=a.attrelid LEFT JOIN pg_attrdef d "
        "ON (d.adrelid,d.adnum)=(a.attrelid,a.attnum) WHERE c.relnamespace='public'::regnamespace "
        "AND a.attnum>0 AND NOT a.attisdropped AND c.relkind IN ('r','p','v') "
        "AND NOT c.relispartition ORDER BY 1,2",
        "SELECT c.relname,conname,pg_get_constraintdef(o.oid) FROM pg_constraint o "
        "JOIN pg_class c ON c.oid=o.conrelid WHERE c.relnamespace='public'::regnamespace "
        "AND NOT c.relispartition ORDER BY 1,2",
        "SELECT c.relname,pg_get_indexdef(i.indexrelid) FROM pg_index i "
        "JOIN pg_class c ON c.oid=i.indrelid WHERE c.relnamespace='public'::regnamespace "
        "AND NOT c.relispartition ORDER BY 1,2",
        "SELECT viewname,definition FROM pg_views WHERE schemaname='public' ORDER BY 1",
        "SELECT proname,pg_get_functiondef(oid) FROM pg_proc "
        "WHERE pronamespace='public'::regnamespace ORDER BY 1",
    ]
    with connect(database) as conn:
        return [conn.execute(query).fetchall() for query in queries]


@pytest.mark.asyncio
async def test_v4_database_migrates_in_place(databases, tmp_path):
    database = databases()
    load_v4(database)
    before = v4_state(database)
    builds = sorted(before["payloads"])
    api_before = await api_bodies(database, builds)
    result = migrate(database)
    assert result["migrated"] and result["calculated_schema_version"] == store.SCHEMA_VERSION
    assert result["from_schema_version"] == 4 and result["schema_version"] == SCHEMA_VERSION == 12
    assert result["from_calculated_schema_version"] == 1
    assert migrate(database)["migrated"] is False
    initialize(database)
    store.initialize(database)
    for run_id, summary in before["summaries"].items():
        assert canonical(read_summary(run_id, database)) == canonical(summary)
    with connect(database) as conn:
        scopes = dict(
            conn.execute("SELECT scope_hash,jsonb_build_array(scope,metrics) FROM scope_details")
        )
        pools = dict(conn.execute("SELECT pool_hash,definition FROM pool_details"))
        extra = conn.execute("SELECT extra FROM pool_stats WHERE adapter IS NOT NULL").fetchone()
        labels = conn.execute("SELECT extra FROM run_buckets WHERE extra IS NOT NULL").fetchall()
        published = conn.execute("SELECT bool_and(published) FROM calculation_builds").fetchone()
        evidence = conn.execute("SELECT * FROM metric_evidence ORDER BY 1").fetchall()
    assert canonical(scopes) == canonical(before["scopes"])
    assert canonical(pools) == canonical(before["pools"])
    assert extra == ({"config": {"future_setting": {"nested": [1, 2.0, None]}}},)
    assert len(labels) == 1 and labels[0][0]["future_counter"] == 3
    assert published == (True,) and evidence == before["evidence"]
    with connect(database) as conn:
        # Stored targets join unscheduled; v5 buckets keep rebuilding without outcome keys.
        assert conn.execute(
            "SELECT count(*),bool_or(census OR pulse_enabled),bool_and(label IS NULL) FROM targets"
        ).fetchone() == (len({p["target"]["id"] for p in before["pools"].values()}), False, True)
        assert conn.execute("SELECT bool_or(outcome_counters) FROM run_buckets").fetchone() == (
            False,
        )
    metrics = {s["scope_hash"]: s["metrics"] for s in status(database)["cached_scopes"]}
    assert canonical(metrics) == canonical({k: v[1] for k, v in before["scopes"].items()})
    for build in builds:
        assert canonical(store.read(database, str(build))) == canonical(before["payloads"][build])
        assert payload_mismatches(database, build) == []
    assert await api_bodies(database, builds) == api_before
    # Migrated data keeps working: replay-safe ingest and a new calculation.
    directory = await measurement(tmp_path, provider="alpha")
    assert ingest(directory, database)["imported"] and not ingest(directory, database)["imported"]
    fresh = calculate(database, POLICY, as_of=store.read(database)["as_of"])
    assert payload_mismatches(database, int(fresh["snapshot_id"])) == []
    # The migrated schema is the schema db-init creates.
    reference = databases()
    store.initialize(reference)
    assert catalog(database) == catalog(reference)


def test_failed_migration_check_rolls_back(databases):
    database = databases()
    load_v4(database)
    with connect(database) as conn:
        # A missing key cannot be told apart from a null one, so the check must fail.
        conn.execute("UPDATE pool_stats SET definition=definition #- '{config,runner_location}'")
    with pytest.raises(ValueError, match="pool definitions"):
        migrate(database)
    with connect(database) as conn:
        assert conn.execute("SELECT version FROM schema_versions").fetchone() == (4,)
        assert conn.execute("SELECT count(definition) FROM pool_stats").fetchone()[0] == 5
        assert conn.execute("SELECT to_regclass('cohorts')").fetchone() == (None,)


def test_migration_requires_unmodified_v4(databases):
    database = databases()
    load_v4(database)
    with connect(database) as conn:
        conn.execute("UPDATE calculation_schema SET checksum='edited'")
    with pytest.raises(ValueError, match="calculated v1"):
        migrate(database)
    with connect(database) as conn:
        conn.execute("UPDATE schema_versions SET version=3")
    with pytest.raises(ValueError, match="unmodified v4"):
        migrate(database)


def test_base_only_v4_database_migrates(databases):
    database = databases()
    load_v4(database)
    with connect(database) as conn:
        run_id = conn.execute("SELECT min(run_id::text) FROM test_runs").fetchone()[0]
        summary = conn.execute(
            "SELECT stored_run_summary(run_key) FROM test_runs WHERE run_id=%s", (run_id,)
        ).fetchone()[0]
        conn.execute(
            "DROP TABLE current_calculation,calculated_snapshots,calculated_overlaps,"
            "calculated_scores,calculated_series,calculated_breakdowns,calculated_windows,"
            "calculation_builds,metric_evidence,calculation_roles,methodologies,calculation_schema"
        )
    assert migrate(database)["calculated_schema_version"] is None
    assert canonical(read_summary(run_id, database)) == canonical(summary)
    store.initialize(database)


def v5_state(database):
    with connect(database) as conn:
        return {
            "summaries": dict(
                conn.execute("SELECT run_id::text,stored_run_summary(run_key) FROM test_runs")
            ),
            "buckets": conn.execute(
                "SELECT run_key,bucket_start,run_bucket_counters(b) FROM run_buckets b ORDER BY 1,2"
            ).fetchall(),
            "scopes": dict(
                conn.execute(
                    "SELECT scope_hash,jsonb_build_array(scope,metrics) FROM scope_details"
                )
            ),
            "pools": dict(conn.execute("SELECT pool_hash,definition FROM pool_details")),
            "payloads": dict(conn.execute("SELECT build_id,payload FROM calculated_snapshots")),
        }


@pytest.mark.asyncio
async def test_v5_database_migrates_in_place(databases, tmp_path):
    database = databases()
    load_v5(database)
    before = v5_state(database)
    builds = sorted(before["payloads"])
    api_before = await api_bodies(database, builds)
    result = migrate(database)
    assert (result["from_schema_version"], result["schema_version"]) == (5, 12)
    assert (result["from_calculated_schema_version"], result["calculated_schema_version"]) == (
        2,
        12,
    )
    assert set(store.V2_CHECKS) <= set(result["verified"])
    assert migrate(database)["migrated"] is False
    initialize(database)
    store.initialize(database)
    assert canonical(v5_state(database)) == canonical(before)
    assert await api_bodies(database, builds) == api_before
    reference = databases()
    store.initialize(reference)
    assert catalog(database) == catalog(reference)
    # The migrated database seeds, links batches and ingests geography buckets.
    seed(database)
    from test_storage_v6 import census

    directory, summary = await census(tmp_path, provider="alpha")
    assert ingest(directory, database)["imported"]
    assert canonical(read_summary(summary["run_id"], database)) == canonical(summary)
    with connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM run_geo_buckets").fetchone() == (2,)


def test_modified_v5_is_refused(databases):
    database = databases()
    load_v5(database)
    with connect(database) as conn:
        conn.execute("UPDATE calculation_schema SET checksum='edited'")
    with pytest.raises(ValueError, match="calculated schema differs"):
        migrate(database)
    with connect(database) as conn:
        conn.execute("UPDATE schema_versions SET checksum='edited'")
    with pytest.raises(ValueError, match="unmodified v4 to v11"):
        migrate(database)
    with connect(database) as conn:
        assert conn.execute("SELECT to_regclass('targets')").fetchone() == (None,)


def test_base_only_v5_database_migrates(databases):
    database = databases()
    load_v5(database)
    with connect(database) as conn:
        summaries = dict(
            conn.execute("SELECT run_id::text,stored_run_summary(run_key) FROM test_runs")
        )
        conn.execute(
            "DROP TABLE current_calculation,calculated_snapshots,calculated_overlaps,"
            "calculated_scores,calculated_series,calculated_breakdowns,calculated_windows,"
            "calculation_builds,metric_evidence,calculation_roles,methodologies,calculation_schema,"
            "places,asn_labels CASCADE"
        )
    assert migrate(database)["calculated_schema_version"] is None
    for run_id, summary in summaries.items():
        assert canonical(read_summary(run_id, database)) == canonical(summary)
    store.initialize(database)


def load_v6_calculated_v2(database):
    """A v6 base schema whose calculated layer is still v2 (a v6 development database)."""
    load_v5(database)
    schema = SCHEMA_V6.read_text()
    assert hashlib.sha256(schema.encode()).hexdigest() == V6_FIXTURE_SHA256
    with connect(database) as conn:
        conn.execute((SOURCE / "migrate_v5_v6.sql").read_text())
        conn.execute(schema)
        conn.execute("DELETE FROM schema_versions")
        conn.execute("INSERT INTO schema_versions(version,checksum) VALUES (6,%s)", (V6_CHECKSUM,))


def load_v6(database):
    """The v6 release's database: v6 base schema and the current calculated v3 layer, with a
    census and a pulse batch."""
    load_v6_calculated_v2(database)
    with connect(database) as conn:
        conn.execute((SOURCE / "calculated/migrate_v2_v3.sql").read_text())
        conn.execute(CALCULATED_V3.read_text())
        conn.execute("DELETE FROM calculation_schema")
        conn.execute("INSERT INTO calculation_schema VALUES (%s,%s)", (3, store.V3_CHECKSUM))
    with connect(database) as conn:
        conn.execute("INSERT INTO runner_regions(region_id,slug,pattern) VALUES (1,'fra1','fra')")
        conn.execute(
            "INSERT INTO countries VALUES ('DE','Germany',1,'03:00') ON CONFLICT DO NOTHING"
        )
        for kind in ("census", "pulse"):
            conn.execute(
                "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,"
                "start_at,status) VALUES (%s,'DE',1,'generic_http',now(),now(),'done')",
                (kind,),
            )


def batches(database):
    with connect(database) as conn:
        return conn.execute(
            "SELECT batch_id,kind,country_code,status,scheduled_for FROM test_batches ORDER BY 1"
        ).fetchall()


def test_v6_database_migrates_through_v7(databases):
    database = databases()
    load_v6(database)
    with pytest.raises(ValueError, match="db-migrate"):
        initialize(database)
    before = batches(database)
    result = migrate(database)
    assert (result["from_schema_version"], result["schema_version"]) == (6, 12)
    assert (result["from_calculated_schema_version"], result["calculated_schema_version"]) == (
        3,
        12,
    )
    assert result["verified"] == [
        *V6_CHECKS,
        *names.V8_CHECKS,
        *store.V3_CHECKS,
        *store.V4_CHECKS,
        *store.V5_CHECKS,
        *store.V6_CHECKS,
        *store.V7_CHECKS,
        *store.V8_CHECKS,
        *store.V9_CHECKS,
        *store.V10_CHECKS,
        *store.V11_CHECKS,
    ]
    assert migrate(database)["migrated"] is False
    initialize(database)
    store.initialize(database)
    assert batches(database) == before
    reference = databases()
    store.initialize(reference)
    assert catalog(database) == catalog(reference)
    with connect(database) as conn:
        # The load test's batches and census reruns fit now.
        retried = conn.execute(
            "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,"
            "start_at,retry_of) SELECT 'load',country_code,region_id,target_id,scheduled_for,"
            "start_at,batch_id FROM test_batches WHERE kind='census' RETURNING retry_of"
        ).fetchone()
        assert retried == (before[0][0],)


def load_v7(database):
    """The v7 release's database: v6 plus migrate_v6_v7.sql and the v7 schema."""
    load_v6(database)
    schema = SCHEMA_V7.read_text()
    assert hashlib.sha256(schema.encode()).hexdigest() == V7_FIXTURE_SHA256
    with connect(database) as conn:
        conn.execute((SOURCE / "migrate_v6_v7.sql").read_text())
        conn.execute(schema)
        conn.execute("DELETE FROM schema_versions")
        conn.execute("INSERT INTO schema_versions(version,checksum) VALUES (7,%s)", (V7_CHECKSUM,))


def test_v7_database_migrates_to_v12(databases):
    database = databases()
    load_v7(database)
    with pytest.raises(ValueError, match="db-migrate"):
        initialize(database)
    before = batches(database)
    result = migrate(database)
    assert (result["from_schema_version"], result["schema_version"]) == (7, 12)
    assert result["verified"] == [
        *names.V8_CHECKS,
        *store.V3_CHECKS,
        *store.V4_CHECKS,
        *store.V5_CHECKS,
        *store.V6_CHECKS,
        *store.V7_CHECKS,
        *store.V8_CHECKS,
        *store.V9_CHECKS,
        *store.V10_CHECKS,
        *store.V11_CHECKS,
    ]
    assert migrate(database)["migrated"] is False
    initialize(database)
    store.initialize(database)
    assert batches(database) == before
    reference = databases()
    store.initialize(reference)
    assert catalog(database) == catalog(reference)
    with connect(database) as conn:
        conn.execute(
            "INSERT INTO load_references(day,region_id,country_code,basis) "
            "VALUES ('2026-10-03',1,'DE','default')"
        )


def load_calculated(database, version):
    """A schema v8 database whose calculated layer is the v4, v5 or v6 release's (from v3 on v7)."""
    load_v7(database)
    fixtures = {
        4: (CALCULATED_V4, store.V4_CHECKSUM),
        5: (CALCULATED_V5, store.V5_CHECKSUM),
        6: (CALCULATED_V6, store.V6_CHECKSUM),
    }
    for path, checksum in fixtures.values():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == checksum
    with connect(database) as conn:
        conn.execute((SOURCE / "migrate_v7_v8.sql").read_text())
        conn.execute(SCHEMA_V8.read_text())
        conn.execute("DELETE FROM schema_versions")
        conn.execute("INSERT INTO schema_versions VALUES (8,%s)", (V8_CHECKSUM,))
        for n in range(4, version + 1):
            conn.execute((SOURCE / f"calculated/migrate_v{n - 1}_v{n}.sql").read_text())
            conn.execute(fixtures[n][0].read_text())
        conn.execute("DELETE FROM calculation_schema")
        conn.execute(
            "INSERT INTO calculation_schema VALUES (%s,%s)", (version, fixtures[version][1])
        )


@pytest.mark.asyncio
async def test_calculated_v4_preserves_published_history_and_enables_city_network_rows(databases):
    database = databases()
    load_calculated(database, 4)
    before = v5_state(database)
    bodies = await api_bodies(database, sorted(before["payloads"]))
    result = migrate(database)
    assert result["from_calculated_schema_version"] == 4
    assert result["calculated_schema_version"] == store.SCHEMA_VERSION
    assert result["verified"] == [
        *names.V8_CHECKS,
        *store.V4_CHECKS,
        *store.V5_CHECKS,
        *store.V6_CHECKS,
        *store.V7_CHECKS,
        *store.V8_CHECKS,
        *store.V9_CHECKS,
        *store.V10_CHECKS,
        *store.V11_CHECKS,
    ]
    assert canonical(v5_state(database)) == canonical(before)
    assert await api_bodies(database, sorted(before["payloads"])) == bodies
    assert migrate(database)["migrated"] is False
    store.initialize(database)
    reference = databases()
    store.initialize(reference)
    assert catalog(database) == catalog(reference)


def breakdowns_by_build(conn):
    return dict(
        conn.execute(
            "SELECT w.build_id,count(*) FROM calculated_breakdown_details "
            "JOIN calculated_windows w USING(result_id) GROUP BY 1"
        )
    )


@pytest.mark.asyncio
async def test_calculated_v5_expires_then_partitions_breakdowns_by_build(databases):
    database = databases()
    load_calculated(database, 5)
    with connect(database) as conn:
        counts = breakdowns_by_build(conn)
        current = conn.execute("SELECT build_id FROM current_calculation").fetchone()[0]
    assert counts[current]
    # On the v5 layer retention runs first, so that the migration moves only the kept builds.
    estimate = store.retention(database, 0, 0, dry_run=True)
    kept, expired = estimate["kept_builds"], estimate["expired_builds"]
    assert estimate["schema"] == "v5"
    assert current in kept and expired
    assert estimate["rows"]["calculated_breakdowns"] == sum(counts.get(b, 0) for b in expired)
    assert store.retention(database, 0, 0) == {
        "schema": "v5",
        "kept_builds": kept,
        "expired_builds": expired,
    }
    before = v5_state(database)
    bodies = await api_bodies(database, kept)
    result = migrate(database)
    assert (result["from_calculated_schema_version"], result["calculated_schema_version"]) == (
        5,
        12,
    )
    assert result["verified"] == [
        *names.V8_CHECKS,
        *store.V5_CHECKS,
        *store.V6_CHECKS,
        *store.V7_CHECKS,
        *store.V8_CHECKS,
        *store.V9_CHECKS,
        *store.V10_CHECKS,
        *store.V11_CHECKS,
    ]
    assert canonical(v5_state(database)) == canonical(before)
    assert await api_bodies(database, kept) == bodies
    with connect(database) as conn:
        # Each kept build's breakdowns moved, all of them, into its windows' lists.
        moved = conn.execute(
            "SELECT w.build_id,count(*) FROM calculated_list_rows "
            "JOIN calculated_windows w USING(list_id) GROUP BY 1"
        ).fetchall()
        assert dict(moved) == {b: counts[b] for b in kept if b in counts}
        assert breakdowns_by_build(conn) == {b: counts[b] for b in kept if b in counts}
    for build in kept:
        assert payload_mismatches(database, build) == []
    assert migrate(database)["migrated"] is False
    store.initialize(database)
    reference = databases()
    store.initialize(reference)
    assert catalog(database) == catalog(reference)


def test_failed_calculated_v5_check_rolls_back(databases, monkeypatch):
    database = databases()
    load_calculated(database, 5)
    monkeypatch.setitem(store.V5_CHECKS, "always", "SELECT 1")
    with pytest.raises(ValueError, match="always"):
        migrate(database)
    with connect(database) as conn:
        assert conn.execute("SELECT version FROM calculation_schema").fetchone() == (5,)
        assert conn.execute(
            "SELECT relkind FROM pg_class WHERE oid='calculated_breakdowns'::regclass"
        ).fetchone() == ("r",)


def breakdown_views(conn):
    """Both breakdown views, row for row, as readers see them."""
    return {
        "details": conn.execute(
            "SELECT * FROM calculated_breakdown_details ORDER BY result_id,dimension,position"
        ).fetchall(),
        "current": conn.execute(
            "SELECT * FROM current_breakdowns ORDER BY result_id,dimension,position"
        ).fetchall(),
    }


PLACE = "jsonb_build_array(country_code,country,region_code,region,city)"
# The distinct place and ASN names of enrichment and the calculated v6 layer together, and the
# calculated places no enrichment location has (country and region rows).
V6_NAMES = (
    f"SELECT (SELECT count(DISTINCT t) FROM (SELECT {PLACE} t FROM enrichment_locations "
    f"UNION ALL SELECT {PLACE} FROM places) u),(SELECT count(DISTINCT t) FROM (SELECT "
    "jsonb_build_array(asn,asn_organization) t FROM enrichment_networks UNION ALL "
    f"SELECT jsonb_build_array(asn,name) FROM asn_labels) u),(SELECT count(*) FROM places "
    f"WHERE {PLACE} NOT IN (SELECT {PLACE} FROM enrichment_locations))"
)
SHARED_IDS = (
    "SELECT (SELECT count(*) FROM place_names p WHERE NOT EXISTS (SELECT FROM "
    "enrichment_locations l WHERE l.place_id=p.place_id)),(SELECT count(DISTINCT place_id) "
    "FROM calculated_list_rows JOIN enrichment_locations USING(place_id)),(SELECT count(*) "
    "FROM asn_labels l WHERE NOT EXISTS (SELECT FROM enrichment_networks n "
    "WHERE n.asn_name_id=l.asn_name_id)),(SELECT max(n) FROM (SELECT count(*) n FROM asn_labels "
    "GROUP BY asn_name_id) a)"
)


@pytest.mark.asyncio
async def test_calculated_v6_moves_breakdown_names_into_the_shared_lists(databases):
    database = databases()
    load_calculated(database, 6)
    with connect(database) as conn:
        # Real-shaped extra: one ASN name under a second usage type.
        assert conn.execute(
            "INSERT INTO asn_labels(asn,name,usage_type) SELECT asn,name,'hosting' FROM asn_labels "
            "WHERE asn IS NOT NULL ORDER BY asn_label_id LIMIT 1 RETURNING asn"
        ).fetchone()
        places, asns, calculated_only = conn.execute(V6_NAMES).fetchone()
        counts, views = breakdowns_by_build(conn), breakdown_views(conn)
    assert calculated_only and counts and all(views.values())
    state = v5_state(database)
    bodies = await api_bodies(database, sorted(state["payloads"]))
    result = migrate(database)
    assert (result["from_calculated_schema_version"], result["calculated_schema_version"]) == (
        6,
        12,
    )
    assert result["verified"] == [
        *names.V8_CHECKS,
        *store.V6_CHECKS,
        *store.V7_CHECKS,
        *store.V8_CHECKS,
        *store.V9_CHECKS,
        *store.V10_CHECKS,
        *store.V11_CHECKS,
    ]
    with connect(database) as conn:
        assert breakdowns_by_build(conn) == counts and breakdown_views(conn) == views
        lists = conn.execute(
            "SELECT (SELECT count(*) FROM place_names),(SELECT count(*) FROM asn_names)"
        ).fetchone()
        only, cities, foreign, usage_types = conn.execute(SHARED_IDS).fetchone()
    # One row per name: breakdown cities and ASN labels use the enrichment's ids, and the
    # calculated-only places join the same list.
    assert lists == (places, asns) and only == calculated_only
    assert cities > 0 and foreign == 0 and usage_types == 2
    assert canonical(v5_state(database)) == canonical(state)
    assert await api_bodies(database, sorted(state["payloads"])) == bodies
    assert migrate(database)["migrated"] is False
    store.initialize(database)
    reference = databases()
    store.initialize(reference)
    assert catalog(database) == catalog(reference)


def test_failed_calculated_v6_check_rolls_back(databases, monkeypatch):
    database = databases()
    load_calculated(database, 6)
    monkeypatch.setitem(store.V6_CHECKS, "always", "SELECT 1")
    with pytest.raises(ValueError, match="always"):
        migrate(database)
    with connect(database) as conn:
        assert conn.execute("SELECT version FROM calculation_schema").fetchone() == (6,)
        assert conn.execute(
            "SELECT to_regclass('places')::text,to_regclass('place_names')"
        ).fetchone() == ("places", None)


def test_failed_v6_check_rolls_back(databases, monkeypatch):
    database = databases()
    load_v6(database)
    monkeypatch.setitem(V6_CHECKS, "always", "SELECT 1")
    with pytest.raises(ValueError, match="always"):
        migrate(database)
    with connect(database) as conn:
        assert conn.execute("SELECT version FROM schema_versions").fetchone() == (6,)
        with pytest.raises(Exception, match="test_batches_kind_check"):
            conn.execute(
                "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,"
                "start_at) VALUES ('load','DE',1,'generic_http',now(),now())"
            )


@pytest.mark.asyncio
async def test_calculated_v2_on_v6_migrates_in_place(databases, tmp_path):
    database = databases()
    load_v6_calculated_v2(database)
    with pytest.raises(ValueError, match="db-migrate"):
        store.initialize(database)
    before = v5_state(database)
    builds = sorted(before["payloads"])
    api_before = await api_bodies(database, builds)
    with connect(database) as conn:
        breakdowns = conn.execute(
            "SELECT result_id,dimension,position,ips,observations,label "
            "FROM calculated_breakdown_details ORDER BY 1,2,3"
        ).fetchall()
    result = migrate(database)
    assert result["from_schema_version"] == 6 and result["from_calculated_schema_version"] == 2
    assert result["verified"] == [
        *store.V2_CHECKS,
        *V6_CHECKS,
        *names.V8_CHECKS,
        *store.V3_CHECKS,
        *store.V4_CHECKS,
        *store.V5_CHECKS,
        *store.V6_CHECKS,
        *store.V7_CHECKS,
        *store.V8_CHECKS,
        *store.V9_CHECKS,
        *store.V10_CHECKS,
        *store.V11_CHECKS,
    ]
    assert migrate(database)["migrated"] is False
    store.initialize(database)
    assert canonical(v5_state(database)) == canonical(before)
    assert await api_bodies(database, builds) == api_before
    with connect(database) as conn:
        assert (
            conn.execute(
                "SELECT result_id,dimension,position,ips,observations,label "
                "FROM calculated_breakdown_details ORDER BY 1,2,3"
            ).fetchall()
            == breakdowns
        )
        assert conn.execute(
            "SELECT count(*) FROM calculated_list_rows WHERE completed IS NOT NULL"
        ).fetchone() == (0,)
        assert conn.execute("SELECT * FROM calculation_schema").fetchall() == [
            (store.SCHEMA_VERSION, store.schema()[1])
        ]
    reference = databases()
    store.initialize(reference)
    assert catalog(database) == catalog(reference)
    # The migrated layer calculates v4 payloads (SNAP-LISTREF-1's lists, NET-SUM-1's networks)
    # whose typed rows rebuild them.
    fresh = calculate(database, POLICY, as_of=store.read(database)["as_of"])
    assert fresh["schema_version"] == 4
    assert payload_mismatches(database, int(fresh["snapshot_id"])) == []


def test_modified_calculated_v2_on_v6_is_refused(databases):
    database = databases()
    load_v6_calculated_v2(database)
    with connect(database) as conn:
        conn.execute("UPDATE calculation_schema SET checksum='edited'")
    with pytest.raises(ValueError, match="calculated schema differs"):
        migrate(database)
    with connect(database) as conn:
        assert conn.execute("SELECT version FROM calculation_schema").fetchone() == (2,)
        assert conn.execute(
            "SELECT count(*) FROM information_schema.columns WHERE table_name="
            "'calculated_windows' AND column_name='uptime'"
        ).fetchone() == (0,)


def test_failed_calculated_check_rolls_back(databases, monkeypatch):
    database = databases()
    load_v5(database)
    monkeypatch.setitem(store.V2_CHECKS, "always", "SELECT 1")
    with pytest.raises(ValueError, match="always"):
        migrate(database)
    with connect(database) as conn:
        assert conn.execute("SELECT version FROM schema_versions").fetchone() == (5,)
        assert conn.execute("SELECT version FROM calculation_schema").fetchone() == (2,)


def load_v8(database):
    """The live shape: the v8 release's database with the calculated v5 release's layer."""
    load_calculated(database, 5)
    with connect(database) as conn:
        # Real-shaped extras: a stored city at a second coordinate, a stored ASN with another ISP.
        assert conn.execute(
            "INSERT INTO enrichment_locations(country_code,country,region_code,region,city,"
            "latitude,longitude) SELECT country_code,country,region_code,region,city,"
            "latitude+0.1,longitude FROM enrichment_locations WHERE city IS NOT NULL "
            "ORDER BY location_id LIMIT 1 RETURNING city"
        ).fetchone()
        assert conn.execute(
            "INSERT INTO enrichment_networks(isp,organization,asn,asn_organization,connection_type,"
            "usage_type) SELECT 'Other ISP',organization,asn,asn_organization,connection_type,"
            "usage_type FROM enrichment_networks WHERE asn IS NOT NULL ORDER BY network_id "
            "LIMIT 1 RETURNING asn"
        ).fetchone()


def v8_rows(conn, locations, networks):
    """Every location, network and per-IP enrichment row as readers see it, and the geo buckets."""
    rows = {
        "locations": conn.execute(f"SELECT * FROM {locations} ORDER BY 1").fetchall(),
        "networks": conn.execute(f"SELECT * FROM {networks} ORDER BY 1").fetchall(),
    }
    rows["ip enrichment"] = conn.execute(
        "SELECT * FROM ip_enrichment_fields ORDER BY ip_id,release_id"
    ).fetchall()
    rows["details"] = conn.execute(
        "SELECT * FROM ip_enrichment_details ORDER BY ip,dbip_release"
    ).fetchall()
    rows["geo"] = conn.execute(
        "SELECT run_key,bucket_start,location_id,network_id,completed FROM run_geo_buckets "
        "ORDER BY 1,2,3,4"
    ).fetchall()
    return rows


# A database created before the framework rename records the same v8 schema under its old checksum.
@pytest.mark.asyncio
@pytest.mark.parametrize("recorded", [V8_CHECKSUM, V8_PRE_RENAME_CHECKSUM])
async def test_v8_database_moves_names_into_shared_lists(databases, tmp_path, recorded):
    database = databases()
    load_v8(database)
    with connect(database) as conn:
        conn.execute("UPDATE schema_versions SET checksum=%s", (recorded,))
    with pytest.raises(ValueError, match="db-migrate"):
        initialize(database)
    with connect(database) as conn:
        before = v8_rows(conn, "enrichment_locations", "enrichment_networks")
        counts = breakdowns_by_build(conn)
        # The lists also take the calculated layer's own places and ASN names.
        places, asns, _ = conn.execute(V6_NAMES).fetchone()
    state = v5_state(database)
    bodies = await api_bodies(database, sorted(state["payloads"]))
    result = migrate(database)
    assert (result["from_schema_version"], result["schema_version"]) == (8, 12)
    assert (result["from_calculated_schema_version"], result["calculated_schema_version"]) == (
        5,
        12,
    )
    assert result["verified"] == [
        *names.V8_CHECKS,
        *store.V5_CHECKS,
        *store.V6_CHECKS,
        *store.V7_CHECKS,
        *store.V8_CHECKS,
        *store.V9_CHECKS,
        *store.V10_CHECKS,
        *store.V11_CHECKS,
    ]
    assert migrate(database)["migrated"] is False
    initialize(database)
    store.initialize(database)
    with connect(database) as conn:
        after = v8_rows(conn, "enrichment_location_fields", "enrichment_network_fields")
        assert breakdowns_by_build(conn) == counts and counts
        lists = conn.execute(
            "SELECT (SELECT count(*) FROM place_names),(SELECT count(*) FROM asn_names)"
        ).fetchone()
        # The extra coordinate and ISP share their city's and ASN's names.
        shared = conn.execute(
            "SELECT (SELECT max(n) FROM (SELECT count(*) n FROM enrichment_locations "
            "GROUP BY place_id) l),(SELECT max(n) FROM (SELECT count(*) n FROM "
            "enrichment_networks GROUP BY asn_name_id) a)"
        ).fetchone()
    assert after == before and lists == (places, asns) and min(shared) >= 2
    # Two coordinates of one city: more enrichment locations than enrichment place names.
    assert len(before["locations"]) > len({row[1:6] for row in before["locations"]})
    assert len(before["ip enrichment"]) > 0
    assert canonical(v5_state(database)) == canonical(state)
    assert await api_bodies(database, sorted(state["payloads"])) == bodies
    reference = databases()
    store.initialize(reference)
    assert catalog(database) == catalog(reference)
    directory = await measurement(tmp_path, provider="alpha")
    assert ingest(directory, database)["imported"]


def test_failed_v8_check_rolls_back(databases, monkeypatch):
    database = databases()
    load_v8(database)
    monkeypatch.setitem(names.V8_CHECKS, "always", "SELECT 1")
    with pytest.raises(ValueError, match="always"):
        migrate(database)
    with connect(database) as conn:
        assert conn.execute("SELECT version FROM schema_versions").fetchone() == (8,)
        assert conn.execute("SELECT to_regclass('place_names')").fetchone() == (None,)
        assert conn.execute(
            "SELECT count(*) FROM information_schema.columns WHERE table_name="
            "'enrichment_locations' AND column_name='city'"
        ).fetchone() == (1,)


def load_v9(database):
    """The live shape: the v9 release's database (its exact schema) with the current calculated
    layer (calculated v7, then v8's parts table and v9's lists)."""
    load_calculated(database, 6)
    schema = SCHEMA_V9.read_text()
    assert hashlib.sha256(schema.encode()).hexdigest() == V9_CHECKSUM
    calculated, calculated_checksum = store.schema()
    with connect(database) as conn:
        conn.execute((SOURCE / "migrate_v8_v9.sql").read_text())
        conn.execute(store.V6_SNAPSHOT)
        conn.execute((SOURCE / "calculated/migrate_v6_v7.sql").read_text())
        conn.execute((SOURCE / "calculated/migrate_v7_v8.sql").read_text())
        conn.execute((SOURCE / "calculated/migrate_v8_v9.sql").read_text())
        conn.execute(schema)
        conn.execute(calculated)
        conn.execute("UPDATE schema_versions SET version=9,checksum=%s", (V9_CHECKSUM,))
        conn.execute(
            "UPDATE calculation_schema SET version=%s,checksum=%s",
            (store.SCHEMA_VERSION, calculated_checksum),
        )


def test_v9_database_adds_estimated_hours(databases):
    database = databases()
    load_v9(database)
    with pytest.raises(ValueError, match="db-migrate"):
        initialize(database)
    with connect(database) as conn:
        runs = conn.execute("SELECT * FROM test_runs ORDER BY run_key").fetchall()
        assert conn.execute("SELECT to_regclass('estimated_hours')").fetchone() == (None,)
    result = migrate(database)
    assert (result["from_schema_version"], result["schema_version"]) == (9, 12)
    assert result["verified"] == []
    assert migrate(database)["migrated"] is False
    initialize(database)
    store.initialize(database)
    reference = databases()
    store.initialize(reference)
    assert catalog(database) == catalog(reference)
    with connect(database) as conn:
        assert conn.execute("SELECT * FROM test_runs ORDER BY run_key").fetchall() == runs and runs
        conn.execute(
            "INSERT INTO estimated_hours(product_id,country_code,day,hour,run_key,measured_at,"
            "measured_ips,measured_requests,hour_factor,unique_ips,requests,day_unique_ips,curve,"
            "formula_version) SELECT product_id,'DE','2026-11-02',5,run_key,started_at,10,100,"
            "0.5,5,50,20,'country',1 FROM test_runs JOIN scope_stats USING(scope_id) "
            "ORDER BY run_key LIMIT 1"
        )
        with pytest.raises(Exception, match="estimated_hours_hour_check"):
            conn.execute(
                "INSERT INTO estimated_hours SELECT product_id,country_code,day,24,run_key,"
                "measured_at,measured_ips,measured_requests,hour_factor,unique_ips,requests,"
                "day_unique_ips,curve,formula_version FROM estimated_hours"
            )
