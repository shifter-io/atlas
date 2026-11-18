"""Value on the all-targets cost (VALUE-ALL-RANK-1, VALUE-FAIL-1): the card targets' bill at
their standard pages, the lower bound for targets without successes, the rank and the value
proportional to the median bill; the cross-check fixture shared with the site API."""

import copy
from datetime import UTC, datetime, timedelta

import pytest
import value_all_crosscheck
from test_value_rank import peer_values

from atlas.calculated.policy import digest, load, value_basis
from atlas.calculated.scores import PRICE_MISSING, component, product_inputs
from atlas.calculated.value_all import (
    CATALOG,
    COST_MISSING,
    NO_RUN,
    NO_SUCCESSES,
    NOT_MEASURED,
    bill,
    by_rank_all,
    card_targets,
    priced,
    proportional,
    standard_pages,
    target_cost,
    target_costs,
)
from atlas.calculated.window_scoring import pricing

CONFIG, METHOD_ID = load("configs/methodology.toml")
CARDS = card_targets(CONFIG)
# 1,000 tries, 500 successes, 0.5 GB of wire traffic: $2 per 1,000 successes at $2/GB.
METRICS = {"completed": 1000, "successes": 500, "bytes_up": 1e8, "bytes_down": 4e8}
PAGE = 1e6  # raw()'s standard page: 1 MB, so at $1/GB a target's cost is its tries per success


def sized(reason):
    """A reason's bytes per try: a target with tries measured them unless it has no bytes."""
    return PAGE if reason in (NO_SUCCESSES, CATALOG) else None


def raw(costs, price=1.0):
    """A raw value component with one target_cost() per card target: a number is its tries per
    success (its cost at $1/GB, every member's page being 1 MB), else its reason."""
    found = {
        t: [c, None, PAGE] if isinstance(c, float) else [None, c, sized(c)]
        for t, c in zip(CARDS, costs, strict=True)
    }
    return component(price, inputs={"cost_per_1k": None, "all_targets": found})


def test_the_card_targets_are_the_eight_of_the_all_targets_score():
    # social_web counts for its sources instagram_web and x_web (TARGET-GROUP-SOCIAL-1).
    assert CARDS == [
        "generic_http",
        "generic_https",
        "amazon_pdp",
        "social_web",
        "google_search",
        "cloudflare_challenge",
        "linkedin_public",
        "ai_answers",
    ]


def test_target_cost_keeps_the_tries_per_success_and_the_bytes_per_try():
    assert target_cost(2.0, METRICS) == [2.0, None, 5e5]
    assert target_cost(2.0, METRICS | {"completed": 0}) == [None, NO_RUN, None]
    assert target_cost(2.0, None) == [None, NO_RUN, None]
    assert target_cost(2.0, METRICS | {"successes": 0}) == [None, NO_SUCCESSES, 5e5]
    zero = METRICS | {"bytes_up": 0, "bytes_down": 0}
    assert target_cost(2.0, zero) == [None, NOT_MEASURED, None]
    assert target_cost(2.0, METRICS | {"bytes_down": None}) == [None, NOT_MEASURED, None]
    assert target_cost(None, METRICS) == [None, CATALOG, 5e5]


def costs_of(price, found):
    """One member's priced() costs at its own bytes per try (its own standard pages)."""
    figures = target_costs(price, found, CONFIG)
    return priced(price, figures, standard_pages([figures]))


def test_bill_complete_partial_and_missing():
    found = {t: {"metrics": METRICS} for t in CARDS}
    assert bill(costs_of(2.0, found)) == (pytest.approx(2.0), None, 8, [])
    found["google_search"] = {"metrics": METRICS | {"successes": 0}}
    del found["ai_answers"]
    cost, partial, counted, missing = bill(costs_of(2.0, found))
    assert (cost, partial, counted) == (None, pytest.approx(2.0), 6)
    assert missing == ["google_search", "ai_answers"]
    # Without a price nothing is counted and nothing is missing for a measured reason.
    assert bill(costs_of(None, {t: {"metrics": METRICS} for t in CARDS})) == (
        None,
        None,
        0,
        [],
    )


def test_eighteen_places_score_in_proportion_to_the_median_bill():
    scored = by_rank_all([raw([float(n)] * 8) for n in range(18, 0, -1)], 18)
    first, third, last = scored[17], scored[15], scored[0]
    # The median bill is 9.5: 1 is over 4 times cheaper (100), 3 scores 50 + 50 log4(9.5 / 3).
    assert (first["value"], first["inputs"]["rank"]) == (100, 1)
    assert third["value"] == pytest.approx(91.5741, abs=1e-4) and third["inputs"]["rank"] == 3
    assert last["value"] == pytest.approx(26.9501, abs=1e-4) and last["inputs"]["rank"] == 18
    assert third["inputs"] == {
        "value": 1.0,
        "cost_per_1k_all_targets": 3.0,
        "partial_cost_per_1k_all_targets": None,
        "targets_counted_all_targets": 8,
        "missing_targets_all_targets": [],
        "cost_is_lower_bound": False,
        "browser_window": None,
        "carried_over_all_targets": {},
        "stale": False,
        "page_bytes_all_targets": dict.fromkeys(CARDS, PAGE),
        "rank": 3,
        "ranked": 18,
        "peers": 18,
    }


def test_ties_share_the_rank_and_the_score():
    scored = by_rank_all([raw([1.0] * 8), raw([1.0] * 8), raw([2.0] * 8), raw([3.0] * 8)], 4)
    assert [c["inputs"]["rank"] for c in scored] == [1, 1, 3, 4]
    assert scored[0]["value"] == scored[1]["value"] == pytest.approx(proportional(1.0, 1.5))


def test_each_halving_of_the_bill_is_worth_25_points_from_0_to_100():
    """VALUE-FAIL-1: 50 + 50 x log4(median / bill); the median bill (4) scores 50."""
    scored = by_rank_all([raw([c] * 8) for c in (2.0, 4.0, 8.0)], 3)
    assert [c["value"] for c in scored] == pytest.approx([75, 50, 25])
    assert proportional(0.5, 4.0) == 100 and proportional(64.0, 4.0) == 0
    assert proportional(0.0, 4.0) == 100


def test_a_new_cheapest_member_moves_the_others_little():
    """Under the rank rule a member entering first cost every other one 100 / N points or more;
    the median moves little, so the others' values do too."""
    # The 30d bills of 17 Nov 2026 (production, 19:10), the cheapest joining last.
    bills = (3.3, 4.44, 4.56, 7.49, 9.07, 9.44, 9.54, 9.71, 9.72, 10.06, 10.8, 11.37, 13.23)
    bills += (15.33, 20.07, 23.52, 25.4)
    before = by_rank_all([raw([c] * 8) for c in bills], 17)
    after = by_rank_all([raw([c] * 8) for c in (*bills, 2.73)], 18)
    assert before[2]["inputs"]["rank"] + 1 == after[2]["inputs"]["rank"]
    assert abs(before[2]["value"] - after[2]["value"]) < 0.1


def test_every_failed_try_costs_a_standard_page():
    """VALUE-FAIL-1: a member whose failures move no bytes is no cheaper for it. fails: 1,000
    tries of which 500 succeed at 1 MB and 500 fail with no bytes; steady: 900 of 1,000 succeed,
    every try 1 MB. By its own bytes fails would bill 1 MB per success, steady 1.11 MB."""
    tries = {"completed": 1000, "bytes_up": 0}
    fails = {t: {"metrics": tries | {"successes": 500, "bytes_down": 5e8}} for t in CARDS}
    steady = {t: {"metrics": tries | {"successes": 900, "bytes_down": 1e9}} for t in CARDS}
    members = [
        component(1.0, inputs={"all_targets": target_costs(1.0, f, CONFIG)})
        for f in (fails, steady)
    ]
    scored = by_rank_all(members, 2)
    # Both priced at the median page, 0.75 MB: 0.75 x 2 and 0.75 x 1000 / 900 per 1,000.
    bills = [c["inputs"]["cost_per_1k_all_targets"] for c in scored]
    assert bills == pytest.approx([1.5, 0.75 / 0.9])
    assert [c["inputs"]["rank"] for c in scored] == [2, 1]
    assert scored[0]["inputs"]["page_bytes_all_targets"] == dict.fromkeys(CARDS, 7.5e5)


def test_no_successes_is_priced_at_the_worst_cost_of_the_kind_as_a_lower_bound():
    cheap = raw([1.0] * 8)
    dear = raw([2.0, 2.0, 2.0, 2.0, 9.0, 7.0, 2.0, 2.0])
    gaps = raw([0.5, 0.5, 0.5, 0.5, NO_SUCCESSES, NO_SUCCESSES, 0.5, 0.5])
    scored = by_rank_all([cheap, dear, gaps], 3)
    lower = scored[2]["inputs"]
    # gaps: (6 x 0.5 + 9 + 7) / 8 = 2.375 (dear's 9 and 7 are the worst); dear: 28 / 8.
    assert lower["cost_per_1k_all_targets"] == pytest.approx(19 / 8)
    assert lower["cost_is_lower_bound"] is True
    assert lower["partial_cost_per_1k_all_targets"] == pytest.approx(0.5)
    assert lower["targets_counted_all_targets"] == 6
    assert lower["missing_targets_all_targets"] == ["google_search", "cloudflare_challenge"]
    assert [c["inputs"]["rank"] for c in scored] == [1, 3, 2]
    assert [c["value"] for c in scored] == pytest.approx(
        [proportional(c, 19 / 8) for c in (1.0, 28 / 8, 19 / 8)]
    )
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
    value = product_inputs(counts, {"basis_per_gb": 2.0}, CONFIG, "residential", window=window)
    found = value["value"]["inputs"]["all_targets"]
    assert list(found) == CARDS
    assert found["generic_http"] == [pytest.approx(2.0), None, 5e5]
    assert found["ai_answers"] == [None, NO_RUN, None]


def test_a_missing_basis_price_is_unpriced_never_the_lowest_price():
    """COST-BASIS-200GB-1: costs use the first plan of 200 GB or more; without that evidence the
    product is null with no_basis_price, even with a lowest price."""
    counts = {
        "unique_ips": 0,
        "datacenter_asn_share": None,
        "geo_accuracy": None,
        "daily": dict.fromkeys(("unique_ips", "countries", "regions", "cities", "asns"), 0),
    }
    window = {"metrics": METRICS, "targets": {"generic_http": {"metrics": METRICS}}}
    prices = {"lowest_per_gb": 1.0, "basis_per_gb": None}
    value = product_inputs(counts, prices, CONFIG, "residential", window=window)["value"]
    assert (value["value"], value["reason"]) == (None, "no_basis_price")
    assert value["inputs"]["cost_per_1k"] is None
    assert value["inputs"]["all_targets"]["generic_http"] == [None, CATALOG, 5e5]
    priced = product_inputs(counts, prices | {"basis_per_gb": 2.5}, CONFIG, "residential")
    assert priced["value"]["value"] == 2.5


def test_pricing_names_the_basis_plan():
    """The basis kind follows the plan GB: 200 or more is first_200gb_plus."""
    as_of = datetime(2026, 11, 10, tzinfo=UTC)

    def evidence(metric, value):
        return {
            "product_id": 1,
            "cohort": "*",
            "metric": metric,
            "value": value,
            "period_start": as_of - timedelta(days=2),
            "valid_until": as_of + timedelta(days=50),
            "source": "fixture",
        }

    for gb, kind in ((200, "first_200gb_plus"), (10000, "first_200gb_plus"), (51, None)):
        rows = [evidence("price_per_gb_basis", 2.85), evidence("price_basis_plan_gb", gb)]
        found, used = pricing(rows, 1, "*", as_of)
        assert (found["basis_per_gb"], found["basis_plan_gb"]) == (2.85, gb)
        assert found["price_basis"] == (kind or "largest_below_200gb") and len(used) == 2
    assert pricing([], 1, "*", as_of)[0]["price_basis"] is None


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
