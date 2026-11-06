"""In-place v4 → … → v12, v5 → … → v12, v6 → … → v12, v7 → … → v12, v8 → … → v12 and v9 → … → v12
migrations against a pg_dump of a v4 database (migration_cases.py builds each release's database).

Every path ends at schema v12 with calculated v12; test_migration_calculated.py migrates databases
whose calculated layer is older than their base schema's, and test_migration_v11.py and _v12.py
start at v10, v11.
"""

import pytest
from calculated_cases import payload_mismatches
from migration_cases import (
    POLICY,
    V6_NAMES,
    api_bodies,
    breakdowns_by_build,
    canonical,
    catalog,
    load_v4,
    load_v5,
    load_v6,
    load_v7,
    load_v8,
    load_v9,
    v5_state,
)
from test_storage import measurement

from proxy_index import names
from proxy_index.calculated import store
from proxy_index.calculated.calculator import calculate
from proxy_index.storage import (
    SCHEMA_VERSION,
    V6_CHECKS,
    V8_CHECKSUM,
    V8_PRE_RENAME_CHECKSUM,
    connect,
    ingest,
    initialize,
    migrate,
    read_summary,
    seed,
    status,
)

pytest_plugins = ["test_storage"]


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
