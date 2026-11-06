"""The calculated store: evidence, the prepared-only API, atomic calculation failures, the
typed rows behind a build, cohorts and the pruning of unpublished payloads."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from calculated_cases import POLICY, benchmark_scenario, payload_mismatches
from psycopg.rows import dict_row
from test_storage import measurement

from proxy_index.calculated import store, store_retention
from proxy_index.calculated.api import app
from proxy_index.calculated.calculator import calculate
from proxy_index.storage import connect, ingest

pytest_plugins = ["test_storage"]


def test_evidence_contract(database):
    store.initialize(database)
    with connect(database) as conn:
        conn.execute("INSERT INTO providers VALUES ('fixture')")
        pid = conn.execute(
            "INSERT INTO products(provider,name) VALUES ('fixture','residential') "
            "RETURNING product_id"
        ).fetchone()[0]
    record = {
        "product_id": pid,
        "cohort": "test",
        "metric": "uptime_pct",
        "value": 99.9,
        "period_start": "2026-09-01T00:00:00Z",
        "period_end": "2026-09-02T00:00:00Z",
        "valid_until": "2026-09-03T00:00:00Z",
        "source": "measurement artifact sha256:fixture",
    }
    assert store.evidence(database, record) == store.evidence(database, record)
    with connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM metric_evidence").fetchone()[0] == 1
    for invalid in (
        record | {"metric": "invented"},
        record | {"metric": "claim_gap", "value": 0.1},  # no longer evidence: measured
        record | {"metric": "blacklist_hit_rate", "value": 0},  # no blocklists anywhere
        record | {"value": float("nan")},
        record | {"value": 101},
        record | {"source": ""},
        {k: v for k, v in record.items() if k != "product_id"} | {"provider": "fixture"},
        {k: v for k, v in record.items() if k != "product_id"}
        | {"provider": "nobody", "product": "residential"},
    ):
        with pytest.raises(ValueError):
            store.evidence(database, invalid)
    # The shipped price list names its product portably and applies to every cohort ("*").
    from proxy_index.calculated.calculator import pricing

    prices = json.loads(Path("configs/pricing/shifter-residential.json").read_text())
    with connect(database) as conn:
        conn.execute("INSERT INTO providers VALUES ('shifter')")
        shifter = conn.execute(
            "INSERT INTO products(provider,name) VALUES ('shifter','residential') "
            "RETURNING product_id"
        ).fetchone()[0]
    hashes = store.evidence(database, prices)
    assert len(hashes) == 2 and store.evidence(database, prices) == hashes
    with connect(database) as conn:
        conn.row_factory = dict_row
        rows = conn.execute("SELECT * FROM metric_evidence ORDER BY evidence_id").fetchall()
    published, used = pricing(rows, shifter, "any-cohort", datetime(2026, 10, 4, tzinfo=UTC))
    assert (published["lowest_per_gb"], published["entry_per_gb"]) == (0.75, 1.0)
    assert len(used) == 2 and published["currency"] == "USD"
    before = pricing(rows, shifter, "c", datetime(2026, 10, 2, tzinfo=UTC))[0]
    assert before["lowest_per_gb"] is None


@pytest.mark.asyncio
async def test_api_is_prepared_only_and_calculation_failure_is_atomic(
    database, tmp_path, monkeypatch
):
    import uuid

    import psycopg
    from psycopg import sql

    from proxy_index.calculated import lists

    store.initialize(database)
    directory = await measurement(tmp_path, requests=5)
    ingest(directory, database)
    raw = json.loads((directory / "summary.json").read_text())
    cutoff = datetime.fromisoformat(raw["finished_at"]) + timedelta(minutes=5)
    first = calculate(database, POLICY, as_of=cutoff, publish=True)
    original = lists.persist

    def fail_after_writing(conn, payload, **signed):
        original(conn, payload, **signed)
        raise ValueError("Injected validation/storage failure")

    monkeypatch.setattr(lists, "persist", fail_after_writing)
    with pytest.raises(ValueError, match="Injected"):
        calculate(database, POLICY, as_of=cutoff + timedelta(minutes=5), publish=True)
    assert store.read(database)["snapshot_id"] == first["snapshot_id"]
    with connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM calculation_builds").fetchone()[0] == 1
    # Serve with a role that can SELECT only the prepared publication tables (snapshot heads and
    # parts, read through their partitioned table, and the current pointer) and, since payload v3
    # (SNAP-LISTREF-1), the list rows its windows reference with their shared names.
    role = "snapshot_reader_" + uuid.uuid4().hex
    with connect(database) as conn:
        conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
        conn.execute(
            sql.SQL(
                "GRANT SELECT ON calculated_snapshots,calculated_snapshot_parts,"
                "current_calculation,calculated_list_rows,place_names,asn_label_names TO {}"
            ).format(sql.Identifier(role))
        )

    def reader(db):
        conn = connect(db)
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        return conn

    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), reader(database) as conn:
            conn.execute("SELECT count(*) FROM ip_observations")
        monkeypatch.setattr(store, "connect", reader)
        async with TestClient(TestServer(app(database))) as client:
            for resource in (
                "products",
                "targets",
                "scores",
                "series",
                "breakdowns",
                "overlap",
                "methodology",
                "fleet",
                "runs",
            ):
                response = await client.get(f"/v1/{resource}?window=24h")
                assert response.status == 200
                assert (await response.json())["snapshot_id"] == first["snapshot_id"]
    finally:
        with connect(database) as conn:
            conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_score_reasons_split_around_derived_block():
    from proxy_index.calculated.lists import split_reasons

    components = {
        "reliability": {"state": "ready", "reason": None},
        "uptime": {"state": "unavailable", "reason": "Needs uptime"},
        "coverage": {"state": "incomplete", "reason": None},
        "value": {"state": "unavailable", "reason": "Needs pricing"},
    }
    score = {
        "components": components,
        "weights": {"reliability": 1, "uptime": 1, "coverage": 2.5, "value": 0},
        "reasons": ["Prefix a", "Prefix b", "Needs uptime", "coverage not ready", "Expired"],
    }
    assert split_reasons(score, True) == [(-2, "Prefix a"), (-1, "Prefix b"), (1, "Expired")]
    # Target scores list no component reasons; everything is explicit.
    assert split_reasons(score, False) == [(i, r) for i, r in enumerate(score["reasons"], 1)]
    with pytest.raises(ValueError, match="rebuilt"):
        split_reasons(score | {"reasons": ["coverage not ready"]}, True)
    with pytest.raises(ValueError, match="differ"):
        split_reasons(score | {"weights": {"reliability": 1}}, True)


@pytest.mark.asyncio
async def test_typed_rows_rebuild_scores_breakdowns_and_cohorts(database, tmp_path, monkeypatch):
    # These assertions read older published builds; retention has its own tests.
    monkeypatch.setattr(store_retention, "RETAIN_PER_DAY", 100)
    policy, last, first = await benchmark_scenario(database, tmp_path)
    ready = calculate(database, policy, as_of=last + timedelta(minutes=10), publish=True)
    later = calculate(database, policy, as_of=last + timedelta(days=2), publish=True)
    scores = ready["products"][0]["windows"]["24h"]["scores"]
    assert scores["balanced"]["state"] == "ready" and scores["balanced"]["rank"] == 1
    week = later["products"][0]["windows"]["7d"]
    assert week["targets"]["generic_http"]["score"]["reasons"] == [
        "Required measurement freshness deadline expired"
    ]
    for build in (first, ready, later):
        assert payload_mismatches(database, int(build["snapshot_id"])) == []
    with connect(database) as conn:
        positions = {
            r[0] for r in conn.execute("SELECT sign(position) FROM calculated_score_reasons")
        }
        assert positions == {-1, 1}
        assert conn.execute("SELECT count(*) FROM cohorts").fetchone()[0] == 1
        # Labels are shared across builds instead of repeated per row: two cities, their
        # (shared) region and country. The cities and the ASN name are the enrichment's own rows.
        assert conn.execute(
            "SELECT count(DISTINCT place_id),count(DISTINCT l.place_id) FROM calculated_list_rows "
            "LEFT JOIN enrichment_locations l USING(place_id)"
        ).fetchone() == (4, 2)
        assert conn.execute(
            "SELECT count(DISTINCT asn_name_id) FROM asn_labels "
            "JOIN enrichment_networks USING(asn_name_id)"
        ).fetchone() == (1,)
        assert conn.execute("SELECT count(*) FROM asn_labels").fetchone()[0] == 1
        # Only referenced texts are stored; "coverage not ready" style reasons are derived.
        assert not conn.execute(
            "SELECT reason FROM score_reasons WHERE reason LIKE '% not ready' OR reason_id NOT IN "
            "(SELECT reason_id FROM calculated_score_reasons UNION "
            "SELECT reason_id FROM calculated_score_components WHERE reason_id IS NOT NULL)"
        ).fetchall()


def test_cohort_criteria_keep_unusual_values(database):
    from psycopg.rows import dict_row

    from proxy_index.calculated.calculator import cohort_for
    from proxy_index.calculated.lists import store_cohort

    store.initialize(database)
    scope = {
        "target": {"id": "generic_http", "url": "http://custom.example/", "version": "1"},
        "config": {
            "concurrency": 4,
            "timeout_seconds": 7.5,
            "requests_per_second": 2.0,
            "provider_config": {"effective_ttl_seconds": "300", "parameters": {"asn": 1}},
        },
    }
    cohort, criteria = cohort_for(scope, "residential")
    with connect(database) as conn:
        conn.row_factory = dict_row
        store_cohort(conn, cohort, criteria | {"future": True})
        row = conn.execute("SELECT * FROM cohorts").fetchone()
        assert row["extra"] == {"ttl_seconds": "300", "future": True}
        assert str(row["rate"]) == "2.0" and row["target_variant"]["url"] == scope["target"]["url"]
        with pytest.raises(ValueError, match="losslessly"):
            store_cohort(conn, "partial", {"kind": "residential"})


@pytest.mark.asyncio
async def test_unpublished_payloads_are_pruned(database, tmp_path, monkeypatch):
    # These assertions read older published builds; retention has its own tests.
    monkeypatch.setattr(store_retention, "RETAIN_PER_DAY", 100)
    store.initialize(database)
    d = await measurement(tmp_path, requests=4)
    ingest(d, database)
    end = datetime.fromisoformat(json.loads((d / "summary.json").read_text())["finished_at"])
    published = calculate(database, POLICY, as_of=end + timedelta(minutes=5), publish=True)
    drafts = [
        calculate(database, POLICY, as_of=end + timedelta(minutes=5 * i))["snapshot_id"]
        for i in range(2, 6)
    ]
    kept = drafts[-store.KEEP_DRAFTS :]
    with connect(database) as conn:
        remaining = [str(r[0]) for r in conn.execute("SELECT build_id FROM calculated_snapshots")]
        assert sorted(remaining) == sorted([published["snapshot_id"], *kept])
        windows = conn.execute("SELECT count(DISTINCT build_id) FROM calculated_windows")
        assert windows.fetchone()[0] == 5  # typed rows of pruned drafts remain
    assert store.read(database, drafts[0]) is None and store.head(database, drafts[0]) is None
    with pytest.raises(ValueError, match="Unknown snapshot"):
        store.publish(database, drafts[1])
    # A pruned draft is recalculated as a new build; publishing protects it from pruning.
    restored = calculate(database, POLICY, as_of=end + timedelta(minutes=10))["snapshot_id"]
    assert int(restored) > int(drafts[-1])
    assert payload_mismatches(database, int(restored)) == []
    with connect(database) as conn:
        builds = conn.execute("SELECT count(*) FROM calculation_builds").fetchone()[0]
        assert builds == 1 + len(drafts)
    store.publish(database, restored)
    calculate(database, POLICY, as_of=end + timedelta(minutes=35))
    calculate(database, POLICY, as_of=end + timedelta(minutes=40))
    assert store.read(database, restored)["snapshot_id"] == restored
    assert store.read(database, published["snapshot_id"]) == published
