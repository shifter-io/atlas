"""In-place migrations of a calculated layer older than its base schema: calculated v2 on a v6
base, and calculated v4, v5 and v6 on a v8 base, each to calculated v12 (migration_cases.py builds
each release's database; test_migration.py migrates the base schema).
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
    load_calculated,
    load_v5,
    load_v6_calculated_v2,
    v5_state,
)

from atlas import names
from atlas.calculated import store
from atlas.calculated.calculator import calculate
from atlas.storage import V6_CHECKS, connect, migrate

pytest_plugins = ["test_storage"]


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
        *store.V12_CHECKS,
        *store.V13_CHECKS,
        *store.V14_CHECKS,
        *store.V15_CHECKS,
        *store.V16_CHECKS,
        *store.V17_CHECKS,
        *store.V18_CHECKS,
        *store.V19_CHECKS,
        *store.V20_CHECKS,
        *store.V21_CHECKS,
        *store.V22_CHECKS,
    ]
    assert canonical(v5_state(database)) == canonical(before)
    assert await api_bodies(database, sorted(before["payloads"])) == bodies
    assert migrate(database)["migrated"] is False
    store.initialize(database)
    reference = databases()
    store.initialize(reference)
    assert catalog(database) == catalog(reference)


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
        23,
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
        *store.V20_CHECKS,
        *store.V21_CHECKS,
        *store.V22_CHECKS,
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
        23,
    )
    assert result["verified"] == [
        *names.V8_CHECKS,
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
        *store.V20_CHECKS,
        *store.V21_CHECKS,
        *store.V22_CHECKS,
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
        *store.V12_CHECKS,
        *store.V13_CHECKS,
        *store.V14_CHECKS,
        *store.V15_CHECKS,
        *store.V16_CHECKS,
        *store.V17_CHECKS,
        *store.V18_CHECKS,
        *store.V19_CHECKS,
        *store.V20_CHECKS,
        *store.V21_CHECKS,
        *store.V22_CHECKS,
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
