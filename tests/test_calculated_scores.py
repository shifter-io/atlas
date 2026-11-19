"""Score building on synthetic inputs: ranks, retention, peer ratios, daily averages,
uniqueness, cohorts and overlap."""

import copy
from datetime import timedelta

import pytest
from calculated_cases import build_inputs, ready_inputs

from atlas.calculated import store
from atlas.calculated.policy import digest


def test_complete_scores_ranks_overrides_and_stale_evidence():
    from atlas.calculated.calculator import build

    c, now, r, b, o, e = ready_inputs()
    result = build(build_inputs(r, b, o, e, now), c, digest(c), 1, None)
    scores = [p["windows"]["24h"]["scores"]["balanced"] for p in result["products"]]
    assert [s["rank"] for s in scores] == [1, 2]
    assert all(s["state"] == "ready" for s in scores)
    assert scores[0]["valid_until"] == now + timedelta(hours=12)
    c["products"]["p1/residential"] = {"weights": dict.fromkeys(c["weights"], 0) | {"speed": 100}}
    changed = build(build_inputs(r, b, o, e, now), c, digest(c), 2, None)
    score = changed["products"][0]["windows"]["24h"]["scores"]["balanced"]
    assert score["score"] == score["components"]["speed"]["value"]
    # Missing pricing cannot silently redistribute value's positive weight.
    c, now, r, b, o, e = ready_inputs()
    e = [v for v in e if v["metric"] != "price_per_gb_lowest"]
    incomplete = build(build_inputs(r, b, o, e, now), c, digest(c), 3, None)
    score = incomplete["products"][0]["windows"]["24h"]["scores"]["balanced"]
    assert score["score"] is None
    assert score["components"]["value"]["reason"] == (
        "Verified effective-dated pricing (lowest $/GB) is required"
    )
    # Without a load test, speed still reads the pulses (SPEED-PULSE-1): the score stands.
    c, now, r, b, o, e = ready_inputs()
    window = without(c, now, (r, b, o, e), lambda x: x["test"] == "load")
    assert window["scores"]["balanced"]["score"] is not None
    assert window["metrics"]["latency_under_load_ms"]["p50"] is None
    assert window["metrics"]["under_load"]["reason"] == "No load test in this window"
    assert window["latency"]["regions"] == []  # no load run, no daily reference choice
    assert window["scores"]["balanced"]["components"]["speed"]["value"] == 100
    # Without a generic_http pulse, speed has no value (its load run alone does not count): no
    # score either, and the product's speed says why, not just that its targets are incomplete.
    window = without(
        c, now, (r, b, o, e), lambda x: x["test"] == "pulse" and x["run_key"] % 10 == 0
    )
    assert window["scores"]["balanced"]["score"] is None
    assert window["metrics"]["latency_under_load_ms"]["p50"] is not None
    speed = window["scores"]["balanced"]["components"]["speed"]
    assert speed["reason"] == "No pulse with enough successes in this window"
    assert speed["value"] is None
    # A product cannot receive a new score from an expired required target.
    later = now + timedelta(days=2)
    for v in e:
        v["period_end"] = later
        v["valid_until"] = later + timedelta(days=1)
    stale = build(build_inputs(r, b, o, e, later), c, digest(c), 4, None)
    assert stale["products"][0]["windows"]["7d"]["scores"]["balanced"]["score"] is None


def without(config, now, inputs, dropped):
    """The first product's 24h window of a build without the runs (and their rows) dropped."""
    from atlas.calculated.calculator import build

    r, b, o, e = inputs
    gone = {x["run_key"] for x in r if dropped(x)}
    result = build(
        build_inputs(
            [x for x in r if x["run_key"] not in gone],
            [x for x in b if x["run_key"] not in gone],
            [x for x in o if x["run_key"] not in gone],
            e,
            now,
        ),
        config,
        digest(config),
        5,
        None,
    )
    return result["products"][0]["windows"]["24h"]


def test_score_retention_and_methodology_change():
    from atlas.calculated.calculator import build

    c, now, r, b, o, e = ready_inputs()
    previous = store.jsonable(build(build_inputs(r, b, o, e, now), c, digest(c), 1, None))
    later = now + timedelta(minutes=5)
    retained = build(build_inputs(r, b, o, [], later), c, digest(c), 2, previous)
    s = retained["products"][0]["windows"]["24h"]["scores"]["balanced"]
    assert s["state"] == "retained" and s["scored_at"] == previous["as_of"]
    assert s["previous_rank"] == 1
    c["weights"]["speed"] += 1
    changed = build(build_inputs(r, b, o, [], later), c, digest(c), 3, previous)
    historical = changed["products"][0]["windows"]["24h"]["scores"]["balanced"]
    assert historical["state"] == "historical" and historical["rank"] is None
    assert historical["methodology_id"] == previous["methodology"]["id"]
    expired = build(build_inputs(r, b, o, [], now + timedelta(days=2)), c, digest(c), 4, previous)
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
    from atlas.calculated.calculator import build

    c, now, r, b, o, e = peer_inputs()
    result = build(build_inputs(r, b, o, e, now), c, digest(c), 1, None)
    p1, p2, p3 = (p["windows"]["24h"] for p in result["products"])
    parts = [w["scores"]["balanced"]["components"] for w in (p1, p2, p3)]

    def values(key):
        return [pytest.approx(c[key]["value"]) for c in parts]

    # Lower is better: p50 latency under load 100, 200 and 300 ms; lowest price per GB.
    assert values("speed") == [100, 50, 100 / 3]
    # Speed is the latency target's (generic_http) alone; generic_https has a display-only
    # light-load latency of its own pulses (HTTPS-LATENCY-1), never a speed.
    assert parts[1]["speed"]["inputs"] == {
        "targets": {"generic_http": {"value": 200, "best": 100}},
        "peers": 3,
    }
    https = p2["targets"]["generic_https"]
    assert https["metrics"]["latency_ms"]["p50"] == pytest.approx(200, rel=0.05)
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
        "basis_per_gb": None,
        "basis_plan_gb": None,
        "price_basis": None,
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
    datacenter = build(build_inputs(r, b, o, e, now), c, digest(c), 2, None)
    dc = datacenter["products"][0]["windows"]["24h"]["scores"]["balanced"]["components"]["dcAsns"]
    assert dc["value"] == 100 and dc["state"] == "ready" and dc["inputs"]["value"] == 0


def test_peer_ratio_retained_scores_and_lone_products():
    from atlas.calculated.calculator import build

    c, now, r, b, o, e = peer_inputs()
    c["minimum_requests"] = 2
    # generic_http also carries the load run: 2 pulse + 2 load requests.
    c["targets"] = {"generic_http": {"minimum_requests": 4}}
    for bucket in b:
        bucket["counters"] |= {"completed": 2, "successes": 2}
        bucket["counters"]["latency_histogram"]["bins"][0][1] = 2
    previous = store.jsonable(build(build_inputs(r, b, o, e, now), c, digest(c), 1, None))
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
    result = build(build_inputs(r, b, o, e, later), c, digest(c), 2, previous)
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
        build_inputs(
            [x for x in r if x["product_id"] == 3],
            [x for x in b if x["run_key"] // 10 == 3],
            [x for x in o if x["run_key"] // 10 == 3],
            [x for x in e if x["product_id"] == 3],
            now,
        ),
        c,
        digest(c),
        3,
        None,
    )
    score = alone["products"][0]["windows"]["24h"]["scores"]["balanced"]
    assert score["score"] == 100 and score["grade"] == "A+" and score["rank"] is None
    assert all(
        v["value"] == 100 and v["inputs"]["peers"] == 1 for v in score["components"].values()
    )
    assert score["components"]["value"]["inputs"] == {"value": 10.0, "best": 10.0, "peers": 1}


def test_daily_average_counts():
    from atlas.calculated.aggregate import daily_average, daily_pools
    from atlas.calculated.calculator import build
    from atlas.calculated.policy import DIMENSIONS

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
    result = build(build_inputs(r, b, o, e, now), c, digest(c), 1, None)
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
    from atlas.calculated.calculator import build

    c, now, r, b, o, e = ready_inputs(products=(1,))
    alone = build(build_inputs(r, b, o, e, now), c, digest(c), 1, None)
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
    partial = build(build_inputs(r, b, first_observations, e, now), c, digest(c), 2, None)
    first, second = partial["products"]
    assert first["windows"]["30d"]["overlap"] == {
        "exclusive_share": 1,
        "shared_share": 0,
        "compared_products": 0,
    }
    assert second["windows"]["30d"]["overlap"]["exclusive_share"] is None

    # Once both pools are observed, their shared IP changes the same calculation.
    compared = build(build_inputs(r, b, o, e, now), c, digest(c), 3, None)
    for product in compared["products"]:
        assert product["windows"]["30d"]["overlap"] == {
            "exclusive_share": 0,
            "shared_share": 1,
            "compared_products": 1,
        }


def test_cohorts_and_duplicate_overlap():
    from atlas.calculated.calculator import build, cohort_for

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
    result = build(build_inputs(r, b, o, e, now), c, digest(c), 1, None)
    w = result["products"][0]["windows"]["7d"]
    assert w["pool"]["unique_ips"] == 1 and w["pool"]["recurring_share"] == 1
    assert w["overlap"]["shared_share"] == 1
    assert len([s for s in w["series"] if s["latency_ms"]["p50"] is not None]) == 1
