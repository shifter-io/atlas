"""In-place v4 → … → v19 and v5 → … → v19 migrations against a pg_dump of a v4 database
(migration_cases.py builds each release's database).

Every path ends at schema v19 with calculated v12; test_migration_v6_to_v9.py starts at v6 to v9,
test_migration_calculated.py migrates databases whose calculated layer is older than their base
schema's, and test_migration_v11.py, _v12.py and test_storage_migrate_v13.py to _v19.py start at
v10 to v15.
"""

import pytest
from calculated_cases import payload_mismatches
from migration_cases import (
    POLICY,
    api_bodies,
    canonical,
    catalog,
    load_v4,
    load_v5,
    v5_state,
)
from test_storage import measurement

from atlas.calculated import store
from atlas.calculated.calculator import calculate
from atlas.storage import (
    SCHEMA_VERSION,
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
    assert result["from_schema_version"] == 4 and result["schema_version"] == SCHEMA_VERSION == 19
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
    assert (result["from_schema_version"], result["schema_version"]) == (5, 19)
    assert (result["from_calculated_schema_version"], result["calculated_schema_version"]) == (
        2,
        18,
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
    with pytest.raises(ValueError, match="unmodified v4 to v18"):
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
