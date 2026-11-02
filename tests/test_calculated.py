import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from helpers import FakeTransport
from psycopg.rows import dict_row
from test_storage import ALL_ATTRIBUTES, enrichment, measurement

from proxy_index.calculated import store
from proxy_index.calculated.aggregate import metrics, pool_lists, target_score
from proxy_index.calculated.api import app
from proxy_index.calculated.calculator import calculate, retain
from proxy_index.calculated.policy import composite, digest, effective, load, ratio
from proxy_index.models import Outcome
from proxy_index.storage import connect, ingest

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
    d = await measurement(tmp_path, requests=10)
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
    # Every request counts whatever the run's role: the benchmark is every request.
    assert w["benchmark"]["completed"] == 5000 and w["roles"]["pool_sampling"] == 5000
    assert w["tests"] == {"census": 0, "pulse": 0, "load": 0, "unscheduled": 5000}
    assert w["pool"]["observations"] == 4997
    assert w["targets"]["generic_https"]["metrics"]["success_rate"] is None
    assert sum(c["ips"] for c in w["pool"]["countries_breakdown"]) == 4967


def ready_inputs(products=(1, 2)):
    """Per product one pulse run per target (US, one time slot) and a load run on generic_http
    in the same slot with the same latency (factor 1: latency under load = light-load latency),
    plus product-wide ("*") uptime and price evidence."""
    config, _ = load(POLICY)
    config["minimum_requests"] = 1
    config["minimum_coverage"] = 0
    config["slots"] = {"min_latency_successes": 1}
    now = datetime(2026, 9, 25, 12, tzinfo=UTC)
    slot = now - timedelta(minutes=5)
    runs = []
    buckets = []
    observations = []
    evidence = []
    for product_id in products:
        for i, (target, test) in enumerate(
            (("generic_http", "pulse"), ("generic_https", "pulse"), ("generic_http", "load"))
        ):
            key = product_id * 10 + i
            scheme = "https" if target == "generic_https" else "http"
            scope = {
                "target": {
                    "id": target,
                    "url": scheme + "://ip-info.com/myip",
                    "version": "1",
                },
                "config": {
                    "concurrency": 1000 if test == "load" else 100,
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
                "role": "monitor",
                "batch_id": key,
                "test": test,
                "slot_at": slot,
                "batch_country": "US",
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
        for metric, value in {"uptime_pct": 100, "price_per_gb_lowest": 5.0}.items():
            evidence.append(
                {
                    "product_id": product_id,
                    "cohort": "*",
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
    from proxy_index.calculated.calculator import build

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
    # Missing pricing cannot silently redistribute value's positive weight.
    c, now, r, b, o, e = ready_inputs()
    e = [v for v in e if v["metric"] != "price_per_gb_lowest"]
    incomplete = build(r, b, o, e, c, digest(c), now, 3, None, None)
    score = incomplete["products"][0]["windows"]["24h"]["scores"]["balanced"]
    assert score["score"] is None
    assert score["components"]["value"]["reason"] == (
        "Verified effective-dated pricing (lowest $/GB) is required"
    )
    # Without a load test, speed has no value: no score either.
    c, now, r, b, o, e = ready_inputs()
    loads = {x["run_key"] for x in r if x["test"] == "load"}
    unloaded = build(
        [x for x in r if x["run_key"] not in loads],
        [x for x in b if x["run_key"] not in loads],
        o,
        e,
        c,
        digest(c),
        now,
        5,
        None,
        None,
    )
    window = unloaded["products"][0]["windows"]["24h"]
    assert window["scores"]["balanced"]["score"] is None
    assert window["metrics"]["latency_under_load_ms"]["p50"] is None
    assert window["metrics"]["under_load"]["reason"] == "No load test in this window"
    (region,) = window["latency"]["regions"]  # these runs name no runner region
    assert region["reference"] is None and region["factors"]["slots"] == 0
    # The product's speed says why, not just that its targets are incomplete.
    speed = window["scores"]["balanced"]["components"]["speed"]
    assert speed["reason"] == "No load test in this window" and speed["value"] is None
    # A product cannot receive a new score from an expired required target.
    later = now + timedelta(days=2)
    for v in e:
        v["period_end"] = later
        v["valid_until"] = later + timedelta(days=1)
    stale = build(r, b, o, e, c, digest(c), later, 4, None, None)
    assert stale["products"][0]["windows"]["7d"]["scores"]["balanced"]["score"] is None


def test_score_retention_and_methodology_change():
    from proxy_index.calculated.calculator import build

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


def peer_inputs():
    """Three residential products with known raw values (see test_peer_ratio_scores)."""
    c, now, r, b, o, e = ready_inputs(products=(1, 2, 3))
    evidence = {
        1: {"uptime_pct": 100, "price_per_gb_lowest": 5.0},
        2: {"uptime_pct": 80, "price_per_gb_lowest": 2.5},
        3: {"uptime_pct": 50, "price_per_gb_lowest": 10.0},
    }
    for v in e:
        v["value"] = evidence[v["product_id"]].get(v["metric"], v["value"])
    first = {v["run_key"]: v for v in o}
    first[10]["usage_type"] = "hosting"  # product 1: all datacenter IPs
    # product 2: a second IP in Germany although the US was asked for (geo accuracy 1/2)
    o.append(
        first[20]
        | {"ip_id": 2, "country_code": "DE", "region": "Bayern", "city": "Munich", "asn": 2}
    )
    o.append(first[30] | {"ip_id": 3, "asn": 3})
    return c, now, r, b, o, e


def test_peer_ratio_scores():
    from proxy_index.calculated.calculator import build

    c, now, r, b, o, e = peer_inputs()
    result = build(r, b, o, e, c, digest(c), now, 1, None, None)
    p1, p2, p3 = (p["windows"]["24h"] for p in result["products"])
    parts = [w["scores"]["balanced"]["components"] for w in (p1, p2, p3)]

    def values(key):
        return [pytest.approx(c[key]["value"]) for c in parts]

    # Lower is better: p50 latency under load 100, 200 and 300 ms; lowest price per GB.
    assert values("speed") == [100, 50, 100 / 3]
    # Speed is the latency target's (generic_http) alone; generic_https has no latency.
    assert parts[1]["speed"]["inputs"] == {
        "targets": {"generic_http": {"value": 200, "best": 100}},
        "peers": 3,
    }
    https = p2["targets"]["generic_https"]
    assert https["metrics"]["latency_ms"]["p50"] is None
    assert https["metrics"]["latency_under_load_ms"]["p50"] is None
    assert https["score"]["components"]["speed"]["value"] is None
    assert https["score"]["weights"]["speed"] == 0
    assert https["score"]["score"] == https["score"]["components"]["reliability"]["value"]
    assert p2["targets"]["generic_http"]["score"]["components"]["speed"]["inputs"] == {
        "value": 200,
        "best": 100,
        "peers": 3,
    }
    assert p2["targets"]["generic_http"]["score"]["score"] == pytest.approx(0.8 * 100 + 0.2 * 50)
    assert values("value") == [50, 100, 25]
    assert parts[0]["value"]["inputs"] == {"value": 5.0, "best": 2.5, "peers": 3}
    assert result["products"][0]["pricing"] == {
        "currency": "USD",
        "lowest_per_gb": 5.0,
        "entry_per_gb": None,
        "effective_from": now - timedelta(days=40),
        "valid_until": now + timedelta(hours=12),
        "sources": ["fixture"],
    }
    # Higher is better: uptime, authenticity, coverage (unique IPs per day), clean ASN share.
    assert values("reliability") == [100, 100, 100]
    assert values("uptime") == [100, 80, 50]
    assert parts[2]["uptime"]["inputs"] == {"value": 50, "best": 100, "peers": 3}
    # Authenticity: 100 x share of exit IPs in the country asked for (p2: one of two).
    assert values("authenticity") == [100, 50, 100]
    assert parts[1]["authenticity"]["inputs"] == {"value": 50, "best": 100, "peers": 3}
    assert p2["pool"]["geo_accuracy"] == 0.5
    assert values("coverage") == [50, 100, 100]
    assert values("dcAsns") == [0, 100, 100]
    # Diversity mixes the dimension ratios: p3 has one country, region and city but two ASNs.
    assert values("diversity") == [50, 100, 0.3 * 50 + 0.2 * 50 + 0.25 * 50 + 0.25 * 100]
    assert parts[2]["diversity"]["inputs"] == {
        "countries": {"value": 1, "best": 2},
        "regions": {"value": 1, "best": 2},
        "cities": {"value": 1, "best": 2},
        "asns": {"value": 2, "best": 2},
        "peers": 3,
    }
    assert p3["pool"]["daily"] == {
        "days": 1,
        "unique_ips": 2,
        "countries": 1,
        "regions": 1,
        "cities": 1,
        "asns": 2,
    }
    weights = c["weights"]
    for w, components in zip((p1, p2, p3), parts, strict=True):
        score = w["scores"]["balanced"]
        assert score["state"] == "ready" and score["score"] == pytest.approx(
            sum(components[k]["value"] * weights[k] for k in weights) / sum(weights.values())
        )
    assert [w["scores"]["balanced"]["rank"] for w in (p1, p2, p3)] == [2, 1, 3]
    # Datacenter products keep 100 on dcAsns whatever their datacenter share.
    for i in (1, 2, 3):
        c["products"][f"p{i}/residential"] = {"kind": "datacenter"}
    datacenter = build(r, b, o, e, c, digest(c), now, 2, None, None)
    dc = datacenter["products"][0]["windows"]["24h"]["scores"]["balanced"]["components"]["dcAsns"]
    assert dc["value"] == 100 and dc["state"] == "ready" and dc["inputs"]["value"] == 0


def test_peer_ratio_retained_scores_and_lone_products():
    from proxy_index.calculated.calculator import build

    c, now, r, b, o, e = peer_inputs()
    c["minimum_requests"] = 2
    # generic_http also carries the load run: 2 pulse + 2 load requests.
    c["targets"] = {"generic_http": {"minimum_requests": 4}}
    for bucket in b:
        bucket["counters"] |= {"completed": 2, "successes": 2}
        bucket["counters"]["latency_histogram"]["bins"][0][1] = 2
    previous = store.jsonable(build(r, b, o, e, c, digest(c), now, 1, None, None))
    # Later, product 3 is the fastest but has too few requests: its score is retained and its
    # latency does not become the best of the others.
    later = now + timedelta(minutes=5)
    for bucket in b:
        if bucket["run_key"] // 10 == 3:
            bucket["counters"] = bucket["counters"] | {
                "completed": 1,
                "successes": 1,
                "latency_histogram": {"bins": [[50, 1]]},
            }
    result = build(r, b, o, e, c, digest(c), later, 2, None, previous)
    p1, p2, p3 = (p["windows"]["24h"] for p in result["products"])
    assert p3["scores"]["balanced"]["state"] == "retained"
    assert p3["targets"]["generic_http"]["score"]["state"] == "retained"
    speed = p1["scores"]["balanced"]["components"]["speed"]
    assert speed["value"] == 100 and speed["inputs"]["peers"] == 2
    assert speed["inputs"]["targets"]["generic_http"] == {"value": 100, "best": 100}
    assert p2["scores"]["balanced"]["components"]["speed"]["value"] == 50
    # Not ready, p3's own speed is still shown against the ready leader (clamped to 100).
    own = p3["targets"]["generic_http"]["score"]["components"]["speed"]
    assert own == {
        "value": 100,
        "state": "incomplete",
        "reason": None,
        "inputs": {"value": 50, "best": 100, "peers": 2},
    }
    # A product alone in its kind is its own leader: 100 on every ready component.
    c, now, r, b, o, e = peer_inputs()
    alone = build(
        [x for x in r if x["product_id"] == 3],
        [x for x in b if x["run_key"] // 10 == 3],
        [x for x in o if x["run_key"] // 10 == 3],
        [x for x in e if x["product_id"] == 3],
        c,
        digest(c),
        now,
        3,
        None,
        None,
    )
    score = alone["products"][0]["windows"]["24h"]["scores"]["balanced"]
    assert score["score"] == 100 and score["grade"] == "A+" and score["rank"] is None
    assert all(
        v["value"] == 100 and v["inputs"]["peers"] == 1 for v in score["components"].values()
    )
    assert score["components"]["value"]["inputs"] == {"value": 10.0, "best": 10.0, "peers": 1}


def test_daily_average_counts():
    from proxy_index.calculated.aggregate import daily_average, daily_pools
    from proxy_index.calculated.calculator import build
    from proxy_index.calculated.policy import DIMENSIONS

    c, now, r, b, o, e = ready_inputs()
    template = o[0]
    # Product 1 sees one new IP a day for 30 days; product 2 one IP on a single day.
    o = [
        template
        | {
            "ip_id": 100 + day,
            "bucket_start": now - timedelta(days=day, minutes=5),
            "first_seen": now - timedelta(days=day, minutes=4),
            "last_seen": now - timedelta(days=day, minutes=3),
        }
        for day in range(30)
    ] + [o[1]]
    b += [b[0] | {"bucket_start": now - timedelta(days=day, minutes=5)} for day in range(1, 30)]
    result = build(r, b, o, e, c, digest(c), now, 1, None, None)
    p1, p2 = result["products"]
    month = p1["windows"]["30d"]["pool"]
    assert month["unique_ips"] == 30
    assert month["daily"] == {
        "days": 30,
        "unique_ips": 1,
        "countries": 1,
        "regions": 1,
        "cities": 1,
        "asns": 1,
    }
    assert p1["windows"]["7d"]["pool"]["daily"]["days"] == 7
    assert p2["windows"]["30d"]["pool"]["daily"]["days"] == 1
    # Compared per day, the 30-day network does not out-cover the one measured for a day.
    for p in (p1, p2):
        coverage = p["windows"]["30d"]["scores"]["balanced"]["components"]["coverage"]
        assert coverage["value"] == 100
        assert coverage["inputs"] == {"value": 1, "best": 1, "peers": 2}
    # A day is [end - 24h, end): a bucket exactly 24 hours before the cutoff is the first day.
    edge = [template | {"bucket_start": now - timedelta(hours=24), "requested_country": None}]
    assert [d is not None for d in daily_pools(edge, now, 2)] == [True, False]
    assert daily_average([None, {"unique_ips": 4, **dict.fromkeys(DIMENSIONS, 2)}], 24)["days"] == 0


def test_single_network_uniqueness_uses_observed_ips_and_measured_peers():
    from proxy_index.calculated.calculator import build

    c, now, r, b, o, e = ready_inputs(products=(1,))
    alone = build(r, b, o, e, c, digest(c), now, 1, None, None)
    for window in alone["products"][0]["windows"].values():
        assert window["pool"]["unique_ips"] == 1
        assert window["overlap"] == {
            "exclusive_share": 1,
            "shared_share": 0,
            "compared_products": 0,
        }

    c, now, r, b, o, e = ready_inputs()
    # A second product with requests but no observed IPs is not a measured peer.
    first_observations = [obs for obs in o if obs["run_key"] == 10]
    partial = build(r, b, first_observations, e, c, digest(c), now, 2, None, None)
    first, second = partial["products"]
    assert first["windows"]["30d"]["overlap"] == {
        "exclusive_share": 1,
        "shared_share": 0,
        "compared_products": 0,
    }
    assert second["windows"]["30d"]["overlap"]["exclusive_share"] is None

    # Once both pools are observed, their shared IP changes the same calculation.
    compared = build(r, b, o, e, c, digest(c), now, 3, None, None)
    for product in compared["products"]:
        assert product["windows"]["30d"]["overlap"] == {
            "exclusive_share": 0,
            "shared_share": 1,
            "compared_products": 1,
        }


def test_cohorts_and_duplicate_overlap():
    from proxy_index.calculated.calculator import build, cohort_for

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

    from proxy_index.calculated import calculator

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


BREAKDOWN_COUNTERS = (
    "ips",
    "observations",
    "completed",
    "successes",
    "success_rate",
    "challenges",
    "blocks",
    "p50",
    "p90",
    "p99",
)
VIEW_NAMES = {"city": "geography"}


def payload_mismatches(database, build_id):
    """Payload fragments that the typed calculated rows of a build do not rebuild exactly."""
    canon = lambda value: json.dumps(value, sort_keys=True)  # noqa: E731
    payload = store.read(database, str(build_id))
    with connect(database) as conn:
        windows = {
            tuple(r[1:]): r[0]
            for r in conn.execute(
                "SELECT result_id,product_id,cohort,target,window_hours,readiness "
                "FROM calculated_windows WHERE build_id=%s",
                (build_id,),
            )
        }
        scores = {
            tuple(r[:2]): canon(r[2:])
            for r in conn.execute(
                "SELECT result_id,profile,components,reasons,weights FROM calculated_score_details"
            )
        }
        breakdowns = {}
        for r in conn.execute(
            "SELECT result_id,dimension,label,ips,observations,completed,successes,success_rate,"
            "challenges,blocks,p50,p90,p99 FROM calculated_breakdown_details "
            "ORDER BY result_id,dimension,position"
        ):
            counters = dict(zip(BREAKDOWN_COUNTERS, r[3:], strict=True))
            if payload["schema_version"] == 1:
                counters = {k: counters[k] for k in ("ips", "observations")}
            breakdowns.setdefault(r[:2], []).append(r[2] | counters)
        criteria = dict(conn.execute("SELECT cohort,criteria FROM cohort_criteria"))
    missing = []
    for p in payload["products"]:
        if canon(criteria[p["cohort"]]) != canon(p["criteria"]):
            missing.append(("criteria", p["cohort"]))
        for w in p["windows"].values():
            key = (p["product_id"], p["cohort"], "", w["period"]["hours"], w["readiness"])
            for target, profile, s in [("", k, v) for k, v in w["scores"].items()] + [
                (t, "balanced", v["score"]) for t, v in w["targets"].items()
            ]:
                result = windows[key[:2] + (target,) + key[3:]]
                if scores[(result, profile)] != canon(
                    [s["components"], s["reasons"], s.get("weights")]
                ):
                    missing.append(("score", result, profile))
            if payload["schema_version"] == 1:
                expected = [
                    ("geography", w["pool"]["geo"]),
                    ("country", w["pool"]["countries_breakdown"]),
                    ("asn", w["pool"]["asns_breakdown"]),
                ]
            else:
                # v2: every breakdown row is typed; the v1 pool lists derive from them. The
                # payload keeps the top rows of each list and its full row count (slim.py);
                # payloads without counts kept every row.
                expected = [(VIEW_NAMES.get(n, n), rows) for n, rows in w["breakdowns"].items()]
                full = {
                    n: breakdowns.get((windows[key], VIEW_NAMES.get(n, n)), [])
                    for n in w["breakdowns"]
                }
                counts = w.get("breakdown_entries", {n: len(r) for n, r in w["breakdowns"].items()})
                if counts != {n: len(r) for n, r in full.items()}:
                    missing.append(("breakdown entries", windows[key]))
                lists = w["pool"].get("list_entries", {})
                if any(
                    w["pool"][k] != v[: len(w["pool"][k])]
                    or lists.get(k, len(w["pool"][k])) != len(v)
                    for k, v in pool_lists(full).items()
                ):
                    missing.append(("pool lists", windows[key]))
            for dimension, rows in expected:
                typed = breakdowns.get((windows[key], dimension), [])
                if payload["schema_version"] == 2:
                    typed = typed[: len(rows)]
                if canon(typed) != canon(rows):
                    missing.append((dimension, windows[key]))
    return missing


def test_score_reasons_split_around_derived_block():
    from proxy_index.calculated.calculator import split_reasons

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


def lenient_policy(tmp_path):
    policy = tmp_path / "methodology.toml"
    policy.write_text(
        POLICY.read_text()
        .replace("minimum_requests = 4000", "minimum_requests = 1")
        .replace("minimum_coverage = 0.90", "minimum_coverage = 0")
        .replace("min_latency_successes = 30", "min_latency_successes = 1")
    )
    return policy


class OtherIP(FakeTransport):
    async def execute(self, target, *, context=None):
        return Outcome(30.2, ip="9.9.9.9", status=200, response_body_bytes=8)


async def benchmark_scenario(database, tmp_path):
    """Two providers with pulses on both targets and a load run in the same US slot, and
    evidence: ready, ranked, then expired scores."""
    from proxy_index.storage import seed

    store.initialize(database)
    seed(database)
    slot = datetime.fromtimestamp(datetime.now(UTC).timestamp() // 1800 * 1800, UTC)
    with connect(database) as conn:
        batches = {
            (kind, target): conn.execute(
                "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,"
                "start_at,status) SELECT %s,'US',region_id,%s,%s,%s,'done' FROM countries "
                "WHERE country_code='US' RETURNING batch_id",
                (kind, target, slot, slot),
            ).fetchone()[0]
            for kind, target in (
                ("pulse", "generic_http"),
                ("pulse", "generic_https"),
                ("load", "generic_http"),
            )
        }
    last = None
    for provider in ("alpha", "beta"):
        for (kind, target), batch_id in batches.items():
            d = await measurement(
                tmp_path,
                provider=provider,
                target=target,
                requested_country="us",
                concurrency=1000 if kind == "load" else 100,
                transport_factory=OtherIP if provider == "beta" else FakeTransport,
            )
            if target == "generic_http":
                attributes = ALL_ATTRIBUTES | ({"city": "San Jose"} if provider == "beta" else {})
                enrichment(d, attributes=attributes)
            ingest(d, database, batch_id=batch_id)
            raw = json.loads((d / "summary.json").read_text())
            last = max(last or raw["finished_at"], raw["finished_at"])
    policy = lenient_policy(tmp_path)
    last = datetime.fromisoformat(last)
    first = calculate(database, policy, as_of=last + timedelta(minutes=5), publish=True)
    with connect(database) as conn:
        ids = [r[0] for r in conn.execute("SELECT product_id FROM products ORDER BY 1")]
    for product_id in ids:
        for metric, value in {
            "uptime_pct": 99.5,
            "price_per_gb_lowest": 4.0,
        }.items():
            store.evidence(
                database,
                {
                    "product_id": product_id,
                    "cohort": first["products"][0]["cohort"],
                    "metric": metric,
                    "value": value,
                    "period_start": (last - timedelta(days=60)).isoformat(),
                    "period_end": (last + timedelta(days=10)).isoformat(),
                    "valid_until": (last + timedelta(days=20)).isoformat(),
                    "source": "fixture",
                },
            )
    return policy, last, first


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
            "SELECT count(DISTINCT place_id),count(DISTINCT l.place_id) FROM calculated_breakdowns "
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

    from proxy_index.calculated.calculator import cohort_for, store_cohort

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
