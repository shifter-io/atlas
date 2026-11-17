"""In-place v6 → … → v20, v7 → … → v20, v8 → … → v20 and v9 → … → v20 migrations
(migration_cases.py builds each release's database from a pg_dump of a v4 database).

test_migration.py starts at v4 and v5.
"""

import pytest
from migration_cases import (
    V6_NAMES,
    api_bodies,
    breakdowns_by_build,
    canonical,
    catalog,
    load_v6,
    load_v7,
    load_v8,
    load_v9,
    v5_state,
)
from test_storage import measurement

from atlas import names
from atlas.calculated import store
from atlas.storage import (
    V6_CHECKS,
    V8_CHECKSUM,
    V8_PRE_RENAME_CHECKSUM,
    connect,
    ingest,
    initialize,
    migrate,
)

pytest_plugins = ["test_storage"]


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
    assert (result["from_schema_version"], result["schema_version"]) == (6, 21)
    assert (result["from_calculated_schema_version"], result["calculated_schema_version"]) == (
        3,
        20,
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
        *store.V12_CHECKS,
        *store.V13_CHECKS,
        *store.V14_CHECKS,
        *store.V15_CHECKS,
        *store.V16_CHECKS,
        *store.V17_CHECKS,
        *store.V18_CHECKS,
        *store.V19_CHECKS,
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


def test_v7_database_migrates_to_v20(databases):
    database = databases()
    load_v7(database)
    with pytest.raises(ValueError, match="db-migrate"):
        initialize(database)
    before = batches(database)
    result = migrate(database)
    assert (result["from_schema_version"], result["schema_version"]) == (7, 21)
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
        *store.V12_CHECKS,
        *store.V13_CHECKS,
        *store.V14_CHECKS,
        *store.V15_CHECKS,
        *store.V16_CHECKS,
        *store.V17_CHECKS,
        *store.V18_CHECKS,
        *store.V19_CHECKS,
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
    assert (result["from_schema_version"], result["schema_version"]) == (8, 21)
    assert (result["from_calculated_schema_version"], result["calculated_schema_version"]) == (
        5,
        20,
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
        *store.V12_CHECKS,
        *store.V13_CHECKS,
        *store.V14_CHECKS,
        *store.V15_CHECKS,
        *store.V16_CHECKS,
        *store.V17_CHECKS,
        *store.V18_CHECKS,
        *store.V19_CHECKS,
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
    assert (result["from_schema_version"], result["schema_version"]) == (9, 21)
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
