import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from calculated_cases import EDGE, MINUTE, POLICY, benchmark_scenario, payload_mismatches, spread
from psycopg.rows import dict_row
from test_storage import measurement

from proxy_index.calculated import store
from proxy_index.calculated.aggregate import metrics, target_score
from proxy_index.calculated.api import app
from proxy_index.calculated.calculator import calculate, retain
from proxy_index.calculated.policy import composite, digest, effective, load, ratio
from proxy_index.storage import connect, ingest

pytest_plugins = ["test_storage"]

LIVE = Path("results/live-shifter/ef3b5883-bf12-48dc-9759-43b79f937276")


def test_histogram_merge_and_gaps():
    t = datetime(2026, 9, 25, tzinfo=UTC)

    def row(bins):
        n = sum(n for _, n in bins)
        return {
            "bucket_start": t,
            "last_observed": t,
            "counters": {
                "completed": n + 1,
                "successes": n,
                "response_body_bytes": 20,
                "errors": {"timeout": 1},
                "latency_histogram": {"bins": bins},
            },
        }

    m = metrics([row([[10, 9], [1000, 1]]), row([[200, 100]])], 24)
    assert m["latency_ms"]["p50"] == 200  # averaging medians would incorrectly return 105
    assert m["completed"] == 112 and m["errors"] == 2
    assert m["coverage"] == 1 / 288
    assert m["latency_ms"]["mean"] == 191.7  # (9 × 10 + 1000 + 100 × 200) / 110
    assert metrics([], 24)["latency_ms"]["p50"] is None
    assert metrics([], 24)["latency_ms"]["mean"] is None


def test_policy_and_eligibility():
    c, h = load(POLICY)
    assert c["target_weights"] == {"reliability": 80, "speed": 20}
    altered = copy.deepcopy(c)
    altered["products"]["shifter/residential"]["minimum_requests"] = 1
    assert digest(altered) != h
    assert effective(altered, "shifter", "residential")["minimum_requests"] == 1
    assert effective(altered, "other", "residential")["minimum_requests"] == 4000
    assert (
        composite(
            {"a": {"value": 100, "state": "ready"}, "b": {"value": None, "state": "unavailable"}},
            {"a": 80, "b": 20},
        )
        is None
    )
    m = {
        "completed": 5000,
        "coverage": 1 / 288,
        "success_rate": 0.9994,
        "latency_ms": {"p50": 1113},
        "latency_under_load_ms": {"p50": 1670},
    }
    assert target_score(m, c)["state"] == "incomplete"
    m["coverage"] = 1
    s = target_score(m, c)
    # Raw values; peer_scores() turns them into ratios to the leader. Speed is the p50 under
    # load, not the light-load p50.
    assert s["state"] == "ready" and s["weights"] == c["target_weights"]
    assert {k: v["value"] for k, v in s["components"].items()} == {
        "reliability": 0.9994,
        "speed": 1670,
    }
    # Without a load test there is no speed value, and the reason says why.
    m["latency_under_load_ms"] = {"p50": None}
    m["under_load"] = {"reason": "No load test in this window"}
    speed = target_score(m, c)["components"]["speed"]
    assert speed["value"] is None and speed["reason"] == "No load test in this window"
    assert [ratio(0.5, 0.8, True), ratio(0.8, 0.8, True), ratio(0.5, 0, True)] == [62.5, 100, 0]
    assert [ratio(200, 100, False), ratio(0, 100, False), ratio(None, 1, True)] == [50, 100, None]


def test_retained_score_expiry():
    now = datetime(2026, 9, 25, tzinfo=UTC)
    prior = {
        "score": 80,
        "grade": "A",
        "scored_at": now.isoformat(),
        "valid_until": (now + timedelta(hours=24)).isoformat(),
        "components": {},
    }
    assert retain({"score": None}, prior, now, 24, None)["state"] == "retained"
    assert retain({"score": None}, prior, now + timedelta(days=2), 24, None)["state"] == "expired"
    fresh = retain({"score": 80, "state": "ready"}, None, now, 24, now - timedelta(days=2))
    assert fresh["state"] == "expired" and fresh["score"] is None
    old = retain({"score": 80, "state": "ready"}, prior, now + timedelta(days=2), 24, now)
    assert old["scored_at"] == prior["scored_at"] and old["state"] == "expired"


@pytest.mark.asyncio
async def test_calculation_publication_and_api(database, tmp_path, monkeypatch):
    # These assertions read older published builds; retention has its own tests.
    monkeypatch.setattr(store, "RETAIN_PER_DAY", 100)
    store.initialize(database)
    # Fixed clocks: both providers' runs in the 12:00 bucket, whenever the test runs.
    d = await spread(tmp_path, monkeypatch, EDGE - 4 * MINUTE, [0] * 10)
    ingest(d, database)
    raw = json.loads((d / "summary.json").read_text())
    end = datetime.fromisoformat(raw["finished_at"]) + timedelta(minutes=5)
    first = calculate(database, POLICY, as_of=end, publish=True)
    assert first["products"][0]["windows"]["24h"]["metrics"]["completed"] == 10
    assert first["products"][0]["windows"]["24h"]["pool"]["unique_ips"] <= 10
    assert first["products"][0]["windows"]["24h"]["scores"]["balanced"]["score"] is None
    assert first["products"][0]["windows"]["24h"]["overlap"] == {
        "exclusive_share": 1,
        "shared_share": 0,
        "compared_products": 0,
    }
    assert calculate(database, POLICY, as_of=end)["snapshot_id"] == first["snapshot_id"]
    # A late imported second provider revises the same cutoff, including overlap.
    second_dir = await spread(tmp_path, monkeypatch, EDGE - 3 * MINUTE, [0] * 10, provider="second")
    ingest(second_dir, database)
    second = calculate(database, POLICY, as_of=end, publish=True)
    assert second["snapshot_id"] != first["snapshot_id"]
    assert second["overlap"][0]["shared_ips"] > 0
    assert second["fleet"][0]["unique_ips"] < sum(
        p["windows"]["24h"]["pool"]["unique_ips"] for p in second["products"]
    )
    # Cohort "*": every product group together, each exit IP counted once.
    totals = {f["window"]: f for f in second["fleet"] if f["cohort"] == "*"}
    assert list(totals) == ["24h", "7d", "30d"]
    day = totals["24h"]
    assert (day["products"], day["providers"]) == (2, 2)
    assert day["metrics"]["completed"] == 20
    period = second["periods"]["24h"]
    with connect(database) as conn:
        distinct = conn.execute(
            "SELECT count(DISTINCT ip_id) FROM ip_observations "
            "WHERE bucket_start >= %s AND bucket_start < %s",
            (period["start"], period["end"]),
        ).fetchone()[0]
    assert (
        day["unique_ips"]
        == distinct
        < sum(p["windows"]["24h"]["pool"]["unique_ips"] for p in second["products"])
    )
    assert len(day["series"]) == 288
    assert sum(r["completed"] for r in day["series"]) == 20
    assert max(r["unique_ips"] or 0 for r in day["series"]) <= day["unique_ips"]
    assert [r["at"] for r in totals["30d"]["series"]] == [
        r["at"] for r in second["products"][0]["windows"]["30d"]["series"]
    ]
    assert store.read(database, first["snapshot_id"]) == first
    # Failure, including an attempt to rewind publication, leaves the current pointer intact.
    with pytest.raises(ValueError):
        calculate(database, POLICY, as_of=end - timedelta(days=1), publish=True)
    assert store.read(database)["snapshot_id"] == second["snapshot_id"]
    empty = calculate(database, POLICY, as_of=end + timedelta(days=31))
    assert empty["products"][0]["windows"]["30d"]["metrics"]["completed"] == 0
    assert empty["products"][0]["windows"]["30d"]["overlap"] == {
        "exclusive_share": None,
        "shared_share": None,
        "compared_products": 0,
    }
    # API still serves after removing access to raw facts from the read path.
    async with TestClient(TestServer(app(database))) as client:
        response = await client.get("/v1/snapshots/current")
        assert response.status == 200
        etag = response.headers["ETag"]
        # The bare snapshot is its summary and a page of its product groups, never the payload.
        current = await response.json()
        assert current["snapshot_id"] == second["snapshot_id"]
        assert current["methodology"] == second["methodology"]
        assert [p["id"] for p in current["data"]] == [p["id"] for p in second["products"]]
        assert current["products"] == len(second["products"])
        assert all("windows" not in p for p in current["data"])
        assert (
            await client.get("/v1/snapshots/current", headers={"If-None-Match": etag})
        ).status == 304
        r = await client.get(f"/v1/snapshots/{first['snapshot_id']}/products?window=7d")
        assert (await r.json())["snapshot_id"] == first["snapshot_id"]
        assert (await client.get("/v1/scores?window=2d")).status == 400
        assert (await client.post("/v1/snapshots/current")).status == 405
        assert (await client.get("/v1/snapshots/999999")).status == 404
    # Boundary: the bucket at the end is excluded, the bucket at the start included.
    bucket = datetime.fromtimestamp(int(end.timestamp() // 300) * 300, UTC) - timedelta(minutes=5)
    before = calculate(database, POLICY, as_of=bucket)
    assert before["products"][0]["windows"]["24h"]["metrics"]["completed"] == 0
    at_start = calculate(database, POLICY, as_of=bucket + timedelta(hours=24))
    assert at_start["products"][0]["windows"]["24h"]["metrics"]["completed"] == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("late", [15, 120], ids=["same-bucket", "cutoff-bucket"])
async def test_a_late_provider_in_the_cutoff_bucket_is_in_the_revised_build(
    database, tmp_path, monkeypatch, late
):
    """FLAKY-OVERLAP-1: the first run ends at 12:04:30, so a cutoff 5 minutes later lies in the
    12:05 bucket and the first build stops at 12:05. A second provider imported late, whose run
    ended `late` seconds after the first (12:04:45: the same bucket; 12:06:30: the cutoff's own
    bucket, as when the test crossed a bucket edge on the real clock), is in the revised build
    at the same cutoff and shares the first's exit IP: that bucket closes (its run had ended by
    the cutoff), so the cutoff moves to its end."""
    store.initialize(database)
    start = EDGE - timedelta(seconds=30)
    ingest(await spread(tmp_path, monkeypatch, start, [0] * 10), database)
    end = start + 5 * MINUTE
    assert calculate(database, POLICY, as_of=end, publish=True)["as_of"] == EDGE.isoformat()
    second = start + timedelta(seconds=late)
    ingest(await spread(tmp_path, monkeypatch, second, [0] * 10, provider="second"), database)
    after = calculate(database, POLICY, as_of=end, publish=True)
    shared = {o["window"]: o["shared_ips"] for o in after["overlap"]}
    assert shared == {"24h": 1, "7d": 1, "30d": 1}
    day = {p["provider"]: p["windows"]["24h"]["metrics"]["completed"] for p in after["products"]}
    assert day == {"fixture": 10, "second": 10}
    assert after["as_of"] == (EDGE + 5 * MINUTE * (late > 60)).isoformat()


@pytest.mark.asyncio
async def test_a_calculation_right_after_a_run_includes_it_once(database, tmp_path):
    store.initialize(database)
    d = await measurement(tmp_path, requests=10)
    ingest(d, database)
    finished = datetime.fromisoformat(json.loads((d / "summary.json").read_text())["finished_at"])
    # REAL-CYCLE-1: the cutoff was floored to 5 minutes, before the run's own bucket.
    snap = calculate(database, POLICY, as_of=finished)
    assert datetime.fromisoformat(snap["as_of"]) > finished
    assert snap["products"][0]["windows"]["24h"]["metrics"]["completed"] == 10
    assert snap["fleet"][0]["metrics"]["completed"] == 10
    latest = calculate(database, POLICY)  # now: every stored run has finished
    assert latest["products"][0]["windows"]["24h"]["metrics"]["completed"] == 10
    # A run that ended after the requested time keeps its bucket out of that cutoff.
    with connect(database) as conn:
        conn.execute("UPDATE test_runs SET finished_at=finished_at+interval '10 minutes'")
    earlier = calculate(database, POLICY, as_of=finished)
    assert datetime.fromisoformat(earlier["as_of"]) <= finished
    assert earlier["products"][0]["windows"]["24h"]["metrics"]["completed"] == 0


def test_saved_shifter_reconciliation(database):
    if not LIVE.exists():
        pytest.skip("Private saved Shifter artifact is not in community checkout")
    store.initialize(database)
    ingest(LIVE, database)
    store.assign_role(database, "ef3b5883-bf12-48dc-9759-43b79f937276", "pool_sampling")
    snap = calculate(database, POLICY, as_of="2026-09-25T15:25:00+00:00", publish=True)
    w = snap["products"][0]["windows"]["24h"]
    assert w["metrics"]["completed"] == 5000
    assert w["metrics"]["successes"] == 4997
    assert w["pool"]["unique_ips"] == 4967
    assert w["metrics"]["latency_ms"].pop("mean") is not None
    assert w["metrics"]["latency_ms"] == {"p50": 1113, "p90": 1395, "p95": 1644, "p99": 3292}
    # Every request counts whatever the run's role: the benchmark is every request.
    assert w["benchmark"]["completed"] == 5000 and w["roles"]["pool_sampling"] == 5000
    assert w["tests"] == {"census": 0, "pulse": 0, "load": 0, "unscheduled": 5000}
    assert w["pool"]["observations"] == 4997
    assert w["targets"]["generic_https"]["metrics"]["success_rate"] is None
    assert sum(c["ips"] for c in w["pool"]["countries_breakdown"]) == 4967


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
    monkeypatch.setattr(store, "RETAIN_PER_DAY", 100)
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
    monkeypatch.setattr(store, "RETAIN_PER_DAY", 100)
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
