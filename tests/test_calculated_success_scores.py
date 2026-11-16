"""1-10 success scores of the HTTP targets (SUCCESS-SCORE-CALC-1, calculated/target_scores.py): the
HTTP ranks and their shape, kinds ranked apart, the test-day spread, the browser scores and
browser_overall unchanged beside them, and challenge_overall's weights, groups, rank and
statuses. success_overall, from the daily tests: tests/test_calculated_success_daily.py."""

import copy
import json
from pathlib import Path

import pytest
from test_calculated_target_scores import GROUP, GROUPS, WEIGHTS, own, products, window

from atlas.calculated import target_scores as ts
from atlas.calculated.policy import load

BROWSER = ("amazon_pdp", GROUP, "ai_overview")


def http(rate, completed=1000, challenge=0.0, block=0.0):
    """An HTTP target's entry: the metrics the API's successRate, captchaRate, blockRate and
    requests come from."""
    metrics = {"completed": completed, "success_rate": rate}
    metrics |= {"challenge_rate": challenge, "block_rate": block}
    return {"metrics": metrics, "score": None}


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
    assert list(found[0]["targets"]["generic_http"]["scores"]) == ["success", "challenge", "block"]


def test_http_targets_get_challenge_and_block_scores():
    # CHALLENGE-BLOCK-TIE-1: ranked on challenge_rate and block_rate, lower is better.
    found = [network(0.9, 0.9), network(0.6, 0.6), network(0.3, 0.3), network(0.5, 0.5)]
    rates = [(0.30, 0.0), (0.10, 0.01), (0.20, 0.002), (0.0, 0.0)]
    for w, (c, b) in zip(found, rates, strict=True):
        w["targets"]["generic_http"] = http(0.9, challenge=c, block=b)
    found[3]["targets"]["generic_http"] = http(0.9, 99)
    found[2]["targets"]["generic_https"]["metrics"] |= {"challenge_rate": None}
    ts.score(products(*found), WEIGHTS, GROUPS)
    challenge = [w["targets"]["generic_http"]["scores"]["challenge"] for w in found]
    assert [s["score"] for s in challenge] == [1, 10, 5.5, None]
    assert [s["rate"] for s in challenge[:3]] == [0.30, 0.10, 0.20]
    assert challenge[3]["status"] == ts.NOT_ENOUGH_DATA
    # Blocks 0 to 1%: a tie, every ranked network shares first place.
    block = [w["targets"]["generic_http"]["scores"]["block"] for w in found[:3]]
    assert {(s["score"], s["rank"], s["ranked_of"], s["status"]) for s in block} == {
        (10, 1, 3, ts.RANKED)
    }
    # A null rate (no classified try) is not ranked; the other three tie at 0%.
    https = [w["targets"]["generic_https"]["scores"]["challenge"] for w in found]
    assert https[2]["status"] == ts.NOT_ENOUGH_DATA and https[2]["score"] is None
    assert {(s["score"], s["ranked_of"]) for s in https[:2] + https[3:]} == {(10, 3)}


def test_challenge_and_block_scores_move_no_success_score_or_overall():
    tied = [network(r, 1 - r, r) for r in (0.9, 0.6, 0.3)]
    for w, c in zip(tied, (0.01, 0.02, 0.015), strict=True):
        for target in ts.HTTP_TARGETS:
            w["targets"][target]["metrics"] |= {"challenge_rate": c, "block_rate": c}
    spread = copy.deepcopy(tied)
    for w, c in zip(spread, (0.1, 0.5, 0.9), strict=True):
        for target in ts.HTTP_TARGETS:
            w["targets"][target]["metrics"] |= {"challenge_rate": c, "block_rate": c}
        for entry in w["targets"].values():
            for e in entry.get("browser", {}).get("engines", {}).values():
                e["outcome_rates"] |= {"challenge": c, "block": c}
    ts.score(products(*tied), WEIGHTS, GROUPS)
    ts.score(products(*spread), WEIGHTS, GROUPS)
    for a, b in zip(tied, spread, strict=True):
        assert json.dumps(a["browser_overall"]) == json.dumps(b["browser_overall"])
        for target, entry in a["targets"].items():
            own_a = entry["browser"]["scores"] if "browser" in entry else entry["scores"]
            other = b["targets"][target]
            own_b = other["browser"]["scores"] if "browser" in other else other["scores"]
            assert json.dumps(own_a["success"]) == json.dumps(own_b["success"])
    assert {tied[0]["targets"]["generic_http"]["scores"]["block"]["score"]} == {10}


def test_a_tie_on_an_http_target_is_a_shared_ten():
    found = [network(0.9, 0.5), network(0.6, 0.5), network(0.3, 0.5), network(0.95, 0.5)]
    found[3]["targets"]["generic_http"] = http(0.95, 10)
    found[3]["targets"]["generic_https"] = http(0.5, 10)
    ts.score(products(*found), WEIGHTS, GROUPS)
    # generic_https: every ranked network at 50%, a tie: a shared 10 that counts (SUCCESS-TIE-1).
    https = [scored(w, "generic_https") for w in found[:3]]
    assert {(s["score"], s["rank"], s["ranked_of"]) for s in https} == {(10, 1, 3)}
    assert scored(found[3], "generic_https")["status"] == ts.NOT_ENOUGH_DATA


def test_networks_are_ranked_within_their_kind():
    config, _ = load(Path("configs/methodology.toml"))
    rates = {"residential": (0.9, 0.6, 0.3), "mobile": (0.2, 0.1)}
    entries = [
        {"kind": kind, "windows": {"24h": network(r, r)}} for kind, rs in rates.items() for r in rs
    ]
    for e in entries:
        rate = e["windows"]["24h"]["targets"]["generic_http"]["metrics"]["success_rate"]
        e["windows"]["24h"]["_daily"] = {"generic_http": [1000, round(1000 * rate)]}
    ts.score_networks(entries, config)
    windows = [e["windows"]["24h"] for e in entries]
    assert [scored(w, "generic_http")["score"] for w in windows[:3]] == [10, 5.5, 1]
    assert {scored(w, "generic_http")["status"] for w in windows[3:]} == {ts.TOO_FEW_NETWORKS}
    # success_overall: each kind's best daily-test rate reads 10, 30 points behind is the floor,
    # a tie that shares its places (SUCCESS-DAILY-DISTANCE-1).
    assert [w["success_overall"]["rank"] for w in windows] == [1, 2.5, 2.5, 1, 2]
    assert [w["success_overall"]["score"] for w in windows[3:]] == [10, 1]


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


def test_challenge_overall_weights_http_targets_at_one_and_counts_groups_once():
    # CHALLENGE-OVERALL-1: fewer challenges score higher. amazon_pdp weighs 2 and the group 4;
    # ai_overview, a group source, counts through the group.
    found = [network(0.9, 0.9, 0.9) for _ in range(3)]
    for w, (h, b) in zip(found, ((0.01, 0.05), (0.10, 0.20), (0.30, 0.02)), strict=True):
        for target in ts.HTTP_TARGETS:
            w["targets"][target]["metrics"]["challenge_rate"] = h
        for target in BROWSER:
            w["targets"][target]["browser"]["engines"]["camoufox"]["outcome_rates"]["challenge"] = b
    ts.score(products(*found), WEIGHTS, GROUPS)
    best = found[0]["challenge_overall"]
    want = {"generic_http": 10, "generic_https": 10, "amazon_pdp": 5.5, GROUP: 5.5}
    assert best["targets"] == want
    assert best["weights"] == {"generic_http": 1, "generic_https": 1, "amazon_pdp": 2, GROUP: 4}
    assert list(best) == ["score", "rank", "ranked_of", "status", "targets", "weights"]
    scores = [w["challenge_overall"]["score"] for w in found]
    assert scores == pytest.approx([53 / 8, 17 / 8, 62 / 8])
    assert [w["challenge_overall"]["rank"] for w in found] == [2, 3, 1]
    assert {w["challenge_overall"]["ranked_of"] for w in found} == {3}
    # A product group has no success_overall: only the networks' daily tests make one.
    assert {"success_overall" in w for w in found} == {False}


def test_challenge_overall_without_ranked_challenge_scores_has_no_score():
    found = [network(r, r) for r in (0.9, 0.6, 0.3)] + [network(0.9, 0.9)]
    for target in ts.HTTP_TARGETS:
        found[3]["targets"][target] = http(0.9, 10)
    ts.score(products(*found), WEIGHTS, GROUPS)
    assert found[3]["challenge_overall"] == {
        "score": None,
        "rank": None,
        "ranked_of": None,
        "status": ts.NO_RANKED_TARGETS,
        "targets": {},
        "weights": {},
    }
    assert [w["challenge_overall"]["ranked_of"] for w in found] == [3, 3, 3, None]
