"""1-10 success scores of the HTTP targets and the overall success score across all targets
(SUCCESS-SCORE-CALC-1, calculated/target_scores.py): the HTTP ranks and their shape, the
weights and groups of success_overall, its rank and statuses, kinds ranked apart, the test-day
spread, and the browser scores and browser_overall unchanged beside them."""

import copy
import json
from pathlib import Path

import pytest
from test_calculated_target_scores import GROUP, GROUPS, WEIGHTS, own, products, window

from proxy_index.calculated import target_scores as ts
from proxy_index.calculated.policy import load

BROWSER = ("amazon_pdp", GROUP, "ai_overview")


def http(rate, completed=1000):
    """An HTTP target's entry: the metrics the API's successRate and requests come from."""
    return {"metrics": {"completed": completed, "success_rate": rate}, "score": None}


def network(http_rate, https_rate, browser_rate=None):
    """A window of generic_http and generic_https, with the BROWSER targets at browser_rate."""
    targets = {t: {"camoufox": own(browser_rate, 500)} for t in BROWSER} if browser_rate else {}
    w = window(targets)
    w["targets"]["generic_http"] = http(http_rate)
    w["targets"]["generic_https"] = http(https_rate)
    return w


def scored(w, target):
    return w["targets"][target]["scores"]["success"]


def test_http_targets_are_ranked_on_their_success_rate():
    found = [network(0.95, 0.30), network(0.60, 0.60), network(0.24, 0.90), network(0.99, 0.99)]
    found[3]["targets"]["generic_http"] = http(0.99, 99)
    found[3]["targets"]["generic_https"]["metrics"] = {}  # not measured
    ts.score(products(*found), WEIGHTS, GROUPS)
    assert [scored(w, "generic_http")["score"] for w in found[:3]] == [10, 5.5, 1]
    assert scored(found[0], "generic_http") == {
        "rate": 0.95,
        "score": 10,
        "rank": 1,
        "ranked_of": 3,
        "status": ts.RANKED,
    }
    assert scored(found[3], "generic_http")["status"] == ts.NOT_ENOUGH_DATA
    # generic_https is ranked on its own: three measured networks.
    assert [scored(w, "generic_https")["score"] for w in found[:3]] == [1, 5.5, 10]
    assert "scores" not in found[3]["targets"]["generic_https"]
    assert list(found[0]["targets"]["generic_http"]["scores"]) == ["success"]


def test_success_overall_weights_http_targets_at_one_and_counts_groups_once():
    found = [network(0.9, 0.3, 0.9), network(0.6, 0.6, 0.6), network(0.3, 0.9, 0.3)]
    ts.score(products(*found), WEIGHTS, GROUPS)
    best = found[0]["success_overall"]
    # amazon_pdp weighs 2 and the group 4; ai_overview, a group source, counts through the group.
    assert best["targets"] == {"generic_http": 10, "generic_https": 1, "amazon_pdp": 10, GROUP: 10}
    assert best["weights"] == {"generic_http": 1, "generic_https": 1, "amazon_pdp": 2, GROUP: 4}
    assert best["score"] == pytest.approx((10 + 1 + 10 * 2 + 10 * 4) / 8)
    assert [w["success_overall"]["score"] for w in found] == pytest.approx([8.875, 5.5, 2.125])
    assert [w["success_overall"]["rank"] for w in found] == [1, 2, 3]
    assert {w["success_overall"]["ranked_of"] for w in found} == {3}
    assert {w["success_overall"]["status"] for w in found} == {ts.RANKED}
    assert list(best) == ["score", "rank", "ranked_of", "status", "targets", "weights"]
    assert ts.HTTP_DIFFICULTY == {"generic_http": 1, "generic_https": 1}


def test_unranked_targets_are_left_out_and_no_ranked_target_has_no_score():
    found = [network(0.9, 0.5), network(0.6, 0.5), network(0.3, 0.5), network(0.95, 0.5)]
    found[3]["targets"]["generic_http"] = http(0.95, 10)
    ts.score(products(*found), WEIGHTS, GROUPS)
    # generic_https: every network at 50%, all similar.
    assert scored(found[0], "generic_https")["status"] == ts.ALL_SIMILAR
    assert found[0]["success_overall"]["targets"] == {"generic_http": 10}
    assert found[3]["success_overall"] == {
        "score": None,
        "rank": None,
        "ranked_of": None,
        "status": ts.NO_RANKED_TARGETS,
        "targets": {},
        "weights": {},
    }
    # Only the networks with a score are ranked.
    assert [w["success_overall"]["ranked_of"] for w in found] == [3, 3, 3, None]


def test_networks_are_ranked_within_their_kind():
    config, _ = load(Path("configs/methodology.toml"))
    rates = {"residential": (0.9, 0.6, 0.3), "mobile": (0.2, 0.1)}
    entries = [
        {"kind": kind, "windows": {"24h": network(r, r)}} for kind, rs in rates.items() for r in rs
    ]
    ts.score_networks(entries, config)
    windows = [e["windows"]["24h"] for e in entries]
    assert [scored(w, "generic_http")["score"] for w in windows[:3]] == [10, 5.5, 1]
    assert {scored(w, "generic_http")["status"] for w in windows[3:]} == {ts.TOO_FEW_NETWORKS}
    assert [w["success_overall"]["rank"] for w in windows[:3]] == [1, 2, 3]
    assert {w["success_overall"]["status"] for w in windows[3:]} == {ts.NO_RANKED_TARGETS}


def test_test_day_spread_ranks_all_eighteen_networks():
    # 0.24 to 0.95 over millions of requests: the standard error is tiny, 3 points decide.
    found = [
        {"targets": {t: http(0.24 + i * 0.71 / 17, 2_000_000) for t in ts.HTTP_TARGETS}}
        for i in range(18)
    ]
    ts.score(products(*found), WEIGHTS, GROUPS)
    for target in ts.HTTP_TARGETS:
        assert {scored(w, target)["status"] for w in found} == {ts.RANKED}
        assert {scored(w, target)["ranked_of"] for w in found} == {18}
    assert {w["success_overall"]["ranked_of"] for w in found} == {18}


def test_browser_scores_and_overall_are_unchanged_by_http_targets():
    with_http = [network(r, 1 - r, r) for r in (0.9, 0.6, 0.3)]
    browser_only = copy.deepcopy(with_http)
    for w in browser_only:
        for target in ts.HTTP_TARGETS:
            w["targets"][target] = {"metrics": {}, "score": None}
    ts.score(products(*with_http), WEIGHTS, GROUPS)
    ts.score(products(*browser_only), WEIGHTS, GROUPS)
    for a, b in zip(with_http, browser_only, strict=True):
        assert json.dumps(a["browser_overall"]) == json.dumps(b["browser_overall"])
        for target in BROWSER:
            assert json.dumps(a["targets"][target]) == json.dumps(b["targets"][target])
        # Without HTTP scores the overall is the browser targets' alone.
        assert set(b["success_overall"]["targets"]) == {"amazon_pdp", GROUP}
