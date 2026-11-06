import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from calculated_cases import EDGE, MINUTE, POLICY, spread
from test_storage import measurement

from proxy_index.calculated import store, store_retention
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
    monkeypatch.setattr(store_retention, "RETAIN_PER_DAY", 100)
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
