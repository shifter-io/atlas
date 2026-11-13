"""Value by place on the all-targets cost (VALUE-ALL-RANK-1): the card targets' bill, the lower
bound for targets without successes, the rank and 100 x (N - rank + 1) / N; the cross-check
fixture shared with the site API."""

import copy

import pytest
import value_all_crosscheck
from test_value_rank import peer_values

from proxy_index.calculated.policy import digest, load, value_basis
from proxy_index.calculated.scores import PRICE_MISSING, component, product_inputs
from proxy_index.calculated.value_all import (
    CATALOG,
    COST_MISSING,
    NO_RUN,
    NO_SUCCESSES,
    NOT_MEASURED,
    bill,
    by_rank_all,
    card_targets,
    target_cost,
    target_costs,
)

CONFIG, METHOD_ID = load("configs/methodology.toml")
CARDS = card_targets(CONFIG)
# 1,000 tries, 500 successes, 0.5 GB of wire traffic: $2 per 1,000 successes at $2/GB.
METRICS = {"completed": 1000, "successes": 500, "bytes_up": 1e8, "bytes_down": 4e8}


def raw(costs, price=1.0):
    """A raw value component with one [cost, reason] per card target (a number is its cost)."""
    found = {
        t: [c, None] if isinstance(c, float) else [None, c]
        for t, c in zip(CARDS, costs, strict=True)
    }
    return component(price, inputs={"cost_per_1k": None, "all_targets": found})


def test_the_card_targets_are_the_eight_of_the_all_targets_score():
    assert CARDS == [
        "generic_http",
        "generic_https",
        "amazon_pdp",
        "instagram_web",
        "google_search",
        "cloudflare_challenge",
        "linkedin_public",
        "ai_answers",
    ]


def test_target_cost_counts_every_wire_byte_over_the_successes():
    assert target_cost(2.0, METRICS) == (pytest.approx(2.0), None)
    assert target_cost(2.0, METRICS | {"completed": 0}) == (None, NO_RUN)
    assert target_cost(2.0, None) == (None, NO_RUN)
    assert target_cost(2.0, METRICS | {"successes": 0}) == (None, NO_SUCCESSES)
    assert target_cost(2.0, METRICS | {"bytes_up": 0, "bytes_down": 0}) == (None, NOT_MEASURED)
    assert target_cost(2.0, METRICS | {"bytes_down": None}) == (None, NOT_MEASURED)
    assert target_cost(None, METRICS) == (None, CATALOG)


def test_bill_complete_partial_and_missing():
    found = {t: {"metrics": METRICS} for t in CARDS}
    assert bill(target_costs(2.0, found, CONFIG)) == (pytest.approx(2.0), None, 8, [])
    found["google_search"] = {"metrics": METRICS | {"successes": 0}}
    del found["ai_answers"]
    cost, partial, counted, missing = bill(target_costs(2.0, found, CONFIG))
    assert (cost, partial, counted) == (None, pytest.approx(2.0), 6)
    assert missing == ["google_search", "ai_answers"]
    # Without a price nothing is counted and nothing is missing for a measured reason.
    assert bill(target_costs(None, {t: {"metrics": METRICS} for t in CARDS}, CONFIG)) == (
        None,
        None,
        0,
        [],
    )


def test_eighteen_places_score_100_88_9_and_5_6():
    scored = by_rank_all([raw([float(n)] * 8) for n in range(18, 0, -1)], 18)
    first, third, last = scored[17], scored[15], scored[0]
    assert (first["value"], first["inputs"]["rank"]) == (100, 1)
    assert third["value"] == pytest.approx(88.888888) and third["inputs"]["rank"] == 3
    assert last["value"] == pytest.approx(5.555555) and last["inputs"]["rank"] == 18
    assert third["inputs"] == {
        "value": 1.0,
        "cost_per_1k_all_targets": 3.0,
        "partial_cost_per_1k_all_targets": None,
        "targets_counted_all_targets": 8,
        "missing_targets_all_targets": [],
        "cost_is_lower_bound": False,
        "rank": 3,
        "ranked": 18,
        "peers": 18,
    }


def test_ties_share_the_rank_and_the_score():
    scored = by_rank_all([raw([1.0] * 8), raw([1.0] * 8), raw([2.0] * 8), raw([3.0] * 8)], 4)
    assert [c["inputs"]["rank"] for c in scored] == [1, 1, 3, 4]
    assert [c["value"] for c in scored] == [100, 100, 50, 25]


def test_no_successes_is_priced_at_the_worst_cost_of_the_kind_as_a_lower_bound():
    cheap = raw([1.0] * 8)
    dear = raw([2.0, 2.0, 2.0, 2.0, 9.0, 7.0, 2.0, 2.0])
    gaps = raw([0.5, 0.5, 0.5, 0.5, NO_SUCCESSES, NO_SUCCESSES, 0.5, 0.5])
    scored = by_rank_all([cheap, dear, gaps], 3)
    lower = scored[2]["inputs"]
    # gaps: (6 x 0.5 + 9 + 7) / 8 = 2.375 (dear's 9 and 7 are the worst); dear: 28 / 8 = 3.5.
    assert lower["cost_per_1k_all_targets"] == pytest.approx(2.375)
    assert lower["cost_is_lower_bound"] is True
    assert lower["partial_cost_per_1k_all_targets"] == pytest.approx(0.5)
    assert lower["targets_counted_all_targets"] == 6
    assert lower["missing_targets_all_targets"] == ["google_search", "cloudflare_challenge"]
    assert [c["inputs"]["rank"] for c in scored] == [1, 3, 2]
    assert [c["value"] for c in scored] == pytest.approx([100, 100 / 3, 200 / 3])
    assert all(c["state"] == "ready" and c["reason"] is None for c in scored)
    assert scored[1]["inputs"]["cost_is_lower_bound"] is False


def test_unpriced_members_are_null_with_their_reason_and_not_in_n():
    no_run = raw([1.0] * 7 + [NO_RUN])
    no_bytes = raw([NOT_MEASURED] + [1.0] * 7)
    nobody = raw([1.0] * 7 + [NO_SUCCESSES])  # no member has a cost on ai_answers
    unpriced = component(None, reason=PRICE_MISSING, inputs={"all_targets": {}})
    alone = raw([2.0] * 8)
    scored = by_rank_all([no_run, no_bytes, unpriced, alone], 4)
    assert [c["inputs"]["rank"] for c in scored] == [None, None, None, 1]
    assert {c["inputs"]["ranked"] for c in scored} == {1}
    assert [c["reason"] for c in scored[:3]] == [COST_MISSING, COST_MISSING, PRICE_MISSING]
    assert scored[0]["state"] == "unavailable" and scored[0]["value"] is None
    assert scored[0]["inputs"]["missing_targets_all_targets"] == ["ai_answers"]
    assert scored[0]["inputs"]["cost_is_lower_bound"] is False
    assert by_rank_all([nobody, raw([NO_SUCCESSES] * 8)], 2)[0]["inputs"]["rank"] is None


def test_without_a_price_there_is_no_lower_bound():
    found = {t: {"metrics": METRICS} for t in CARDS}
    found["google_search"] = {"metrics": METRICS | {"successes": 0}}
    costs = target_costs(None, found, CONFIG)
    unpriced = component(None, reason=PRICE_MISSING, inputs={"all_targets": costs})
    scored = by_rank_all([unpriced, raw([1.0] * 8)], 2)
    assert scored[0]["inputs"]["cost_per_1k_all_targets"] is None
    assert scored[0]["reason"] == PRICE_MISSING


def test_no_price_and_no_successes_anywhere_stays_unpriced():
    found = {t: {"metrics": METRICS | {"successes": 0}} for t in CARDS}
    costs = target_costs(None, found, CONFIG)
    unpriced = component(None, reason=PRICE_MISSING, inputs={"all_targets": costs})
    scored = by_rank_all([unpriced, raw([1.0] * 8)], 2)
    assert scored[0]["inputs"]["rank"] is None and scored[0]["value"] is None
    assert scored[0]["inputs"]["cost_is_lower_bound"] is False
    assert scored[0]["reason"] == PRICE_MISSING
    assert {c["inputs"]["ranked"] for c in scored} == {1}


def test_product_inputs_keep_each_card_target_cost():
    counts = {
        "unique_ips": 0,
        "datacenter_asn_share": None,
        "geo_accuracy": None,
        "daily": dict.fromkeys(("unique_ips", "countries", "regions", "cities", "asns"), 0),
    }
    window = {"metrics": None, "targets": {"generic_http": {"metrics": METRICS}}}
    value = product_inputs(counts, {"lowest_per_gb": 2.0}, CONFIG, "residential", window=window)
    found = value["value"]["inputs"]["all_targets"]
    assert list(found) == CARDS
    assert found["generic_http"] == [pytest.approx(2.0), None]
    assert found["ai_answers"] == [None, NO_RUN]


def test_value_basis_rank_all_targets_and_methodology_id():
    assert value_basis(CONFIG) == "rank_all_targets"
    rank = copy.deepcopy(CONFIG) | {"value": {"basis": "rank"}}
    assert digest(rank) != METHOD_ID


def test_a_build_without_the_card_targets_leaves_value_unpriced():
    # The peer build measures the HTTP targets only: no browser target has a run.
    scored = [p["value"] for p in peer_values("rank_all_targets")]
    assert all(v["value"] is None and v["reason"] == COST_MISSING for v in scored)
    assert {v["inputs"]["ranked"] for v in scored} == {0}
    assert all("ai_answers" in v["inputs"]["missing_targets_all_targets"] for v in scored)


def test_the_cross_check_fixture_is_current():
    """The API's TestValueAllCrossCheck prices the same metrics with billOver."""
    assert value_all_crosscheck.FIXTURE.read_text() == value_all_crosscheck.text()
