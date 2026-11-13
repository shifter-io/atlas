"""Value by place (VALUE-BY-RANK-1): 100 x (N - rank + 1) / N on the cost per 1,000 successful
requests, ties sharing the rank, unpriced members null and not counted; basis "ratio" keeps the
old ratio to the cheapest list price."""

import copy

import pytest
from calculated_cases import build_inputs
from test_calculated_scores import peer_inputs

from proxy_index.calculated.policy import digest, load, value_basis
from proxy_index.calculated.scores import PRICE_MISSING, component, product_inputs
from proxy_index.calculated.value_rank import COST_MISSING, by_rank, cost_per_1k

METRICS = {"completed": 1000, "bytes_up": 1e8, "bytes_down": 4e8, "request_success_rate": 0.5}


def raw(cost, price=1.0):
    return component(price, inputs={"cost_per_1k": cost})


def test_cost_per_1k_is_the_api_wire_cost():
    # 0.5 GB per 1,000 requests at $2/GB, half of them successful: $2 per 1,000 successes.
    assert cost_per_1k(2.0, METRICS) == pytest.approx(2.0)
    older = METRICS | {"request_success_rate": None, "success_rate": 0.25}
    assert cost_per_1k(2.0, older) == pytest.approx(4.0)
    assert cost_per_1k(2.0, METRICS | {"bytes_up": 0, "bytes_down": 0}) == 0
    for missing in (
        {"bytes_up": None},
        {"completed": 0},
        {"request_success_rate": 0.0},
        {"request_success_rate": None, "success_rate": None},
    ):
        assert cost_per_1k(2.0, METRICS | missing) is None
    assert cost_per_1k(None, METRICS) is None and cost_per_1k(2.0, None) is None


def test_eighteen_places_score_100_88_9_and_5_6():
    scored = by_rank([raw(float(n)) for n in range(18, 0, -1)], 18)
    first, third, last = scored[17], scored[15], scored[0]
    assert first["value"] == 100 and first["inputs"]["rank"] == 1
    assert third["value"] == pytest.approx(88.888888) and third["inputs"]["rank"] == 3
    assert last["value"] == pytest.approx(5.555555) and last["inputs"]["rank"] == 18
    assert {c["inputs"]["ranked"] for c in scored} == {18}
    assert third["inputs"] == {
        "value": 1.0,
        "cost_per_1k": 3.0,
        "rank": 3,
        "ranked": 18,
        "peers": 18,
    }
    assert all(c["state"] == "ready" and c["reason"] is None for c in scored)


def test_ties_share_the_rank_and_the_score():
    scored = by_rank([raw(1.0), raw(1.0), raw(2.0), raw(3.0)], 4)
    assert [c["inputs"]["rank"] for c in scored] == [1, 1, 3, 4]
    assert [c["value"] for c in scored] == [100, 100, 50, 25]


def test_a_member_without_a_cost_is_null_and_not_counted():
    unpriced = component(None, reason=PRICE_MISSING, inputs={"cost_per_1k": None})
    unmeasured = raw(None, price=3.0)
    scored = by_rank([raw(1.0), unpriced, raw(2.0), unmeasured], 3)
    assert [c["inputs"]["rank"] for c in scored] == [1, None, 2, None]
    assert [c["value"] for c in scored] == [100, None, 50, None]
    assert {c["inputs"]["ranked"] for c in scored} == {2}
    assert scored[1]["reason"] == PRICE_MISSING and scored[1]["state"] == "unavailable"
    assert scored[3]["reason"] == COST_MISSING and scored[3]["state"] == "unavailable"
    assert scored[3]["inputs"]["value"] == 3.0


def test_product_inputs_prices_1000_successes():
    counts = {
        "unique_ips": 0,
        "datacenter_asn_share": None,
        "geo_accuracy": None,
        "daily": dict.fromkeys(("unique_ips", "countries", "regions", "cities", "asns"), 0),
    }
    config, _ = load("configs/methodology.toml")
    window = {"metrics": METRICS}
    value = product_inputs(counts, {"lowest_per_gb": 2.0}, config, "residential", window=window)
    assert value["value"]["value"] == 2.0
    assert value["value"]["inputs"]["cost_per_1k"] == pytest.approx(2.0)
    bare = product_inputs(counts, {"lowest_per_gb": 2.0}, config, "residential")["value"]
    assert bare["inputs"]["cost_per_1k"] is None


def test_value_basis_setting_and_methodology_id():
    config, method_id = load("configs/methodology.toml")
    assert value_basis(config) == "rank_all_targets"
    ratio = copy.deepcopy(config) | {"value": {"basis": "ratio"}}
    assert digest(ratio) != method_id
    assert value_basis({}) == "ratio"
    for bad in ({"basis": "price"}, {"basis": "rank", "other": 1}, "rank"):
        with pytest.raises(ValueError):
            value_basis({"value": bad})


def peer_values(basis):
    from proxy_index.calculated.calculator import build

    c, now, r, b, o, e = peer_inputs()
    c["value"] = {"basis": basis}
    for bucket in b:  # the same wire bytes per request everywhere: the cost follows the price
        bucket["outcome"] = True
        bucket["counters"] |= {"challenges": 0, "blocks": 0, "bytes_up": 100, "bytes_down": 900}
    result = build(build_inputs(r, b, o, e, now), c, digest(c), 1, None)
    return [p["windows"]["24h"]["scores"]["balanced"]["components"] for p in result["products"]]


def test_peer_scores_by_rank_and_by_ratio():
    ranked = [p["value"] for p in peer_values("rank")]
    costs = [v["inputs"]["cost_per_1k"] for v in ranked]
    assert all(cost is not None for cost in costs)
    # Prices $5, $2.50 and $10 per GB on the same traffic: the cost follows the price.
    assert costs[1] < costs[0] < costs[2]
    assert [v["inputs"]["rank"] for v in ranked] == [2, 1, 3]
    assert [v["value"] for v in ranked] == pytest.approx([200 / 3, 100, 100 / 3])
    assert {v["inputs"]["ranked"] for v in ranked} == {3}
    # The old rule: 100 x cheapest list price / yours.
    ratio = [p["value"] for p in peer_values("ratio")]
    assert [v["value"] for v in ratio] == pytest.approx([50, 100, 25])
    assert ratio[0]["inputs"] == {"value": 5.0, "best": 2.5, "peers": 3}
