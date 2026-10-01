import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_storage import measurement

from milf_framework.calculated import store
from milf_framework.calculated.aggregate import metrics, target_score
from milf_framework.calculated.api import app
from milf_framework.calculated.calculator import calculate, retain
from milf_framework.calculated.policy import composite, digest, effective, load
from milf_framework.storage import connect, ingest

pytest_plugins = ["test_storage"]

POLICY = Path("configs/methodology.toml")
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
    assert metrics([], 24)["latency_ms"]["p50"] is None


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
    }
    assert target_score(m, c)["score"] is None
    m["coverage"] = 1
    s = target_score(m, c)
    assert s["score"] == pytest.approx(
        0.8 * s["components"]["reliability"]["value"] + 0.2 * s["components"]["speed"]["value"]
    )


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
async def test_calculation_publication_and_api(database, tmp_path):
    store.initialize(database)
    d = await measurement(tmp_path, requests=10)
    ingest(d, database)
    raw = json.loads((d / "summary.json").read_text())
    end = datetime.fromisoformat(raw["finished_at"]) + timedelta(minutes=5)
    first = calculate(database, POLICY, as_of=end, publish=True)
    assert first["products"][0]["windows"]["24h"]["metrics"]["completed"] == 10
    assert first["products"][0]["windows"]["24h"]["pool"]["unique_ips"] <= 10
    assert first["products"][0]["windows"]["24h"]["scores"]["balanced"]["score"] is None
    assert first["products"][0]["windows"]["24h"]["overlap"]["exclusive_share"] is None
    assert calculate(database, POLICY, as_of=end)["snapshot_id"] == first["snapshot_id"]
    # A late imported second provider revises the same cutoff, including overlap.
    second_dir = await measurement(tmp_path, requests=10, provider="second")
    ingest(second_dir, database)
    second = calculate(database, POLICY, as_of=end, publish=True)
    assert second["snapshot_id"] != first["snapshot_id"]
    assert second["overlap"][0]["shared_ips"] > 0
    assert second["fleet"][0]["unique_ips"] < sum(
        p["windows"]["24h"]["pool"]["unique_ips"] for p in second["products"]
    )
    assert store.read(database, first["snapshot_id"]) == first
    # Failure, including an attempt to rewind publication, leaves the current pointer intact.
    with pytest.raises(ValueError):
        calculate(database, POLICY, as_of=end - timedelta(days=1), publish=True)
    assert store.read(database)["snapshot_id"] == second["snapshot_id"]
    empty = calculate(database, POLICY, as_of=end + timedelta(days=31))
    assert empty["products"][0]["windows"]["30d"]["metrics"]["completed"] == 0
    # API still serves after removing access to raw facts from the read path.
    async with TestClient(TestServer(app(database))) as client:
        response = await client.get("/v1/snapshots/current")
        assert response.status == 200
        etag = response.headers["ETag"]
        assert (await response.json()) == second
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
    assert w["metrics"]["latency_ms"] == {"p50": 1113, "p90": 1395, "p95": 1644, "p99": 3292}
    assert w["benchmark"]["completed"] == 0
    assert w["pool"]["observations"] == 4997
    assert w["targets"]["generic_https"]["metrics"]["success_rate"] is None
    assert sum(c["ips"] for c in w["pool"]["countries_breakdown"]) == 4967


def ready_inputs():
    from milf_framework.calculated.calculator import cohort_for

    config, _ = load(POLICY)
    config["minimum_requests"] = 1
    config["minimum_coverage"] = 0
    now = datetime(2026, 9, 25, 12, tzinfo=UTC)
    runs = []
    buckets = []
    observations = []
    evidence = []
    for product_id in (1, 2):
        for i, target in enumerate(("generic_http", "generic_https")):
            key = product_id * 10 + i
            scope = {
                "target": {
                    "id": target,
                    "url": ("http" if i == 0 else "https") + "://checkip.amazonaws.com/",
                    "version": "1",
                },
                "config": {
                    "concurrency": 100,
                    "timeout_seconds": 15,
                    "requested_country": "us",
                    "runner_location": "test",
                },
            }
            run = {
                "run_key": key,
                "run_id": str(key),
                "product_id": product_id,
                "provider": f"p{product_id}",
                "name": "residential",
                "role": "benchmark",
                "scope": scope,
                "started_at": now - timedelta(minutes=2),
                "finished_at": now - timedelta(minutes=1),
                "summary": {},
            }
            runs.append(run)
            buckets.append(
                {
                    "run_key": key,
                    "bucket_start": now - timedelta(minutes=5),
                    "counters": {
                        "completed": 1,
                        "successes": 1,
                        "errors": {},
                        "response_body_bytes": 10,
                        "latency_histogram": {"bins": [[100 * product_id, 1]]},
                    },
                }
            )
            if i == 0:
                observations.append(
                    {
                        "run_key": key,
                        "bucket_start": now - timedelta(minutes=5),
                        "ip_id": 1,
                        "first_seen": now - timedelta(minutes=2),
                        "last_seen": now - timedelta(minutes=1),
                        "count": 1,
                        "product_first_seen": now - timedelta(minutes=2),
                        "matched": True,
                        "country_code": "US",
                        "asn": 1,
                        "usage_type": "residential",
                        "ip": "1.1.1.1",
                        "city": "Test",
                        "region": "Test",
                    }
                )
        cohort, _ = cohort_for(scope, "residential")
        for metric, value in {
            "uptime_pct": 100,
            "claim_gap": 0,
            "blacklist_hit_rate": 0,
            "cost_per_1k_success": 0.005,
        }.items():
            evidence.append(
                {
                    "product_id": product_id,
                    "cohort": cohort,
                    "metric": metric,
                    "value": value,
                    "period_start": now - timedelta(days=40),
                    "period_end": now,
                    "valid_until": now + timedelta(hours=12),
                    "source": "fixture",
                }
            )
    return config, now, runs, buckets, observations, evidence


def test_complete_scores_ranks_overrides_and_stale_evidence():
    from milf_framework.calculated.calculator import build

    c, now, r, b, o, e = ready_inputs()
    result = build(r, b, o, e, c, digest(c), now, 1, None, None)
    scores = [p["windows"]["24h"]["scores"]["balanced"] for p in result["products"]]
    assert [s["rank"] for s in scores] == [1, 2]
    assert all(s["state"] == "ready" for s in scores)
    assert scores[0]["valid_until"] == now + timedelta(hours=12)
    c["products"]["p1/residential"] = {"weights": dict.fromkeys(c["weights"], 0) | {"speed": 100}}
    changed = build(r, b, o, e, c, digest(c), now, 2, None, None)
    score = changed["products"][0]["windows"]["24h"]["scores"]["balanced"]
    assert score["score"] == score["components"]["speed"]["value"]
    # Missing reputation cannot silently redistribute authenticity's positive weight.
    c, now, r, b, o, e = ready_inputs()
    e = [v for v in e if v["metric"] != "blacklist_hit_rate"]
    incomplete = build(r, b, o, e, c, digest(c), now, 3, None, None)
    assert incomplete["products"][0]["windows"]["24h"]["scores"]["balanced"]["score"] is None
    # A product cannot receive a new score from an expired required target.
    later = now + timedelta(days=2)
    for v in e:
        v["period_end"] = later
        v["valid_until"] = later + timedelta(days=1)
    stale = build(r, b, o, e, c, digest(c), later, 4, None, None)
    assert stale["products"][0]["windows"]["7d"]["scores"]["balanced"]["score"] is None


def test_score_retention_and_methodology_change():
    from milf_framework.calculated.calculator import build

    c, now, r, b, o, e = ready_inputs()
    previous = store.jsonable(build(r, b, o, e, c, digest(c), now, 1, None, None))
    later = now + timedelta(minutes=5)
    retained = build(r, b, o, [], c, digest(c), later, 2, None, previous)
    s = retained["products"][0]["windows"]["24h"]["scores"]["balanced"]
    assert s["state"] == "retained" and s["scored_at"] == previous["as_of"]
    assert s["previous_rank"] == 1
    c["weights"]["speed"] += 1
    changed = build(r, b, o, [], c, digest(c), later, 3, None, previous)
    historical = changed["products"][0]["windows"]["24h"]["scores"]["balanced"]
    assert historical["state"] == "historical" and historical["rank"] is None
    assert historical["methodology_id"] == previous["methodology"]["id"]
    expired = build(r, b, o, [], c, digest(c), now + timedelta(days=2), 4, None, previous)
    assert expired["products"][0]["windows"]["24h"]["scores"]["balanced"]["state"] == "expired"


def test_cohorts_and_duplicate_overlap():
    from milf_framework.calculated.calculator import build, cohort_for

    c, now, r, b, o, e = ready_inputs()
    base = cohort_for(r[0]["scope"], "residential")[0]
    custom = copy.deepcopy(r[0]["scope"])
    custom["target"]["url"] = "http://custom.example/"
    assert cohort_for(custom, "residential")[0] != base
    for key, value in [
        ("concurrency", 5),
        ("requested_country", "fr"),
        ("runner_location", "other"),
    ]:
        custom = copy.deepcopy(r[0]["scope"])
        custom["config"][key] = value
        assert cohort_for(custom, "residential")[0] != base
    # An IP recurring in many buckets remains one distinct member.
    o.append(o[0] | {"bucket_start": now - timedelta(days=1, minutes=5)})
    result = build(r, b, o, e, c, digest(c), now, 1, None, None)
    w = result["products"][0]["windows"]["7d"]
    assert w["pool"]["unique_ips"] == 1 and w["pool"]["recurring_share"] == 1
    assert w["overlap"]["shared_share"] == 1
    assert len([s for s in w["series"] if s["latency_ms"]["p50"] is not None]) == 1


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
        record | {"value": float("nan")},
        record | {"value": 101},
        record | {"source": ""},
    ):
        with pytest.raises(ValueError):
            store.evidence(database, invalid)


@pytest.mark.asyncio
async def test_api_is_prepared_only_and_calculation_failure_is_atomic(
    database, tmp_path, monkeypatch
):
    import uuid

    import psycopg
    from psycopg import sql

    from milf_framework.calculated import calculator

    store.initialize(database)
    directory = await measurement(tmp_path, requests=5)
    ingest(directory, database)
    raw = json.loads((directory / "summary.json").read_text())
    cutoff = datetime.fromisoformat(raw["finished_at"]) + timedelta(minutes=5)
    first = calculate(database, POLICY, as_of=cutoff, publish=True)
    original = calculator.persist

    def fail_after_writing(conn, payload):
        original(conn, payload)
        raise ValueError("Injected validation/storage failure")

    monkeypatch.setattr(calculator, "persist", fail_after_writing)
    with pytest.raises(ValueError, match="Injected"):
        calculate(database, POLICY, as_of=cutoff + timedelta(minutes=5), publish=True)
    assert store.read(database)["snapshot_id"] == first["snapshot_id"]
    with connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM calculation_builds").fetchone()[0] == 1
    # Serve with a role that can SELECT only the two prepared publication tables.
    role = "snapshot_reader_" + uuid.uuid4().hex
    with connect(database) as conn:
        conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
        conn.execute(
            sql.SQL("GRANT SELECT ON calculated_snapshots,current_calculation TO {}").format(
                sql.Identifier(role)
            )
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
