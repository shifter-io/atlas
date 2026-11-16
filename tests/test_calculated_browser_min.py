"""SCORE-BROWSER-MIN-1 (#1134) and BROWSER-MIN-100-1 (#1232, the owner's decision of 2026-11-16):
a browser target counts in a network's overall scores only with the methodology's
browser_min_requests (100, was 500) completed tries of its engine in the window. Under it the
target is not_enough_data, left out of the overalls and of the target's ranking, so a run cut
short moves no overall and no rank."""

from pathlib import Path

import pytest

from atlas.calculated import target_scores as ts
from atlas.calculated.policy import (
    BROWSER_MIN_REQUESTS,
    browser_min_requests,
    load,
    validate,
)

POLICY = Path("configs/methodology.toml")
CONFIG = load(POLICY)[0]
HTTP = {"a": 0.95, "b": 0.90, "c": 0.85, "d": 0.80}
LINKEDIN = {"a": 0.50, "b": 0.40, "c": 0.30}
WINDOWS = ("24h", "7d", "30d")
# Each number of tries around the minimum and whether the target then counts.
AROUND = [(99, False), (100, True), (101, True)]


def browser(ok, completed):
    """A browser target's entry with Camoufox figures only."""
    own = {"completed": completed, "successes": round(ok * completed)}
    own["outcome_rates"] = {"ok": ok, "challenge": 1 - ok, "block": 0.0}
    lead = {"engine": "camoufox", "outcome_rates": own["outcome_rates"]}
    return {"metrics": {}, "browser": lead | {"engines": {"camoufox": own}}}


def http(rate, completed=10_000):
    metrics = {"completed": completed, "success_rate": rate}
    return {"metrics": metrics | {"challenge_rate": 1 - rate, "block_rate": 0.0}}


def networks(rows, target="linkedin_public"):
    """Four residential networks a to d: generic_http measured well in each window, and
    `target` where rows[name] = {label: (ok, completed)} has it."""
    out = []
    for name, rate in HTTP.items():
        windows = {}
        for label in WINDOWS:
            windows[label] = {"targets": {"generic_http": http(rate)}}
            if label in rows.get(name, {}):
                windows[label]["targets"][target] = browser(*rows[name][label])
        out.append({"id": name, "kind": "residential", "windows": windows})
    ts.score_networks(out, CONFIG)
    return {n["id"]: n["windows"] for n in out}


def full(rates, d=None):
    """networks() with a to c at 600 tries of linkedin_public and d at `d` (ok, completed)."""
    found = {name: {"24h": (rate, 600)} for name, rate in rates.items()}
    return networks(found | ({"d": {"24h": d}} if d else {}))


def overall(found, name, key="challenge_overall", label="24h"):
    """A network's overall: challenge_overall unless `key`, the one browser targets count in
    whatever the success overall reads (SUCCESS-DAILY-DISTANCE-1, #1229)."""
    return found[name][label][key]


def linkedin(found, name, label="24h", target="linkedin_public"):
    return found[name][label]["targets"][target]["browser"]["scores"]


def test_the_owner_set_100_the_same_as_the_http_minimum():
    assert CONFIG["browser_min_requests"] == 100 == browser_min_requests(CONFIG)
    assert BROWSER_MIN_REQUESTS == ts.MIN_REQUESTS == 100
    assert browser_min_requests({}) == ts.MIN_REQUESTS


@pytest.mark.parametrize("value", [0, -1, 2.5, True, "100", None])
def test_the_methodology_refuses_an_invalid_minimum(value):
    with pytest.raises(ValueError, match="Invalid browser_min_requests"):
        validate(CONFIG | {"browser_min_requests": value})


@pytest.mark.parametrize("label", WINDOWS)
@pytest.mark.parametrize(("tries", "counts"), AROUND)
def test_a_browser_target_counts_from_100_tries_in_each_window(label, tries, counts):
    # a to c at exactly 100 tries: one run each, every one of them ranked.
    rows = {name: {label: (rate, 100)} for name, rate in LINKEDIN.items()}
    found = networks(rows | {"d": {label: (0.60, tries)}})
    d = linkedin(found, "d", label)["success"]
    assert d["status"] == (ts.RANKED if counts else ts.NOT_ENOUGH_DATA)
    assert (d["score"] == 10) is counts and (d["score"] is None) is not counts
    weights = overall(found, "d", label=label)["weights"]
    assert weights == ({"generic_http": 1, "linkedin_public": 5} if counts else {"generic_http": 1})
    for name in LINKEDIN:
        assert linkedin(found, name, label)["success"]["ranked_of"] == (4 if counts else 3)
    # The other windows have no LinkedIn tries at all.
    for other in set(WINDOWS) - {label}:
        assert "linkedin_public" not in overall(found, "d", label=other)["weights"]


@pytest.mark.parametrize(("tries", "counts"), AROUND)
def test_a_group_target_counts_from_100_tries(tries, counts):
    rates = {"a": 0.50, "b": 0.40, "c": 0.30, "d": 0.60}
    rows = {name: {"24h": (rate, 100 if name != "d" else tries)} for name, rate in rates.items()}
    found = networks(rows, target="ai_answers")
    d = linkedin(found, "d", target="ai_answers")["success"]
    assert d["status"] == (ts.RANKED if counts else ts.NOT_ENOUGH_DATA)
    # The group counts at its own difficulty (5); its sources never count on their own.
    weights = overall(found, "d")["weights"]
    assert weights == ({"generic_http": 1, "ai_answers": 5} if counts else {"generic_http": 1})
    assert overall(found, "a")["weights"] == {"generic_http": 1, "ai_answers": 5}
    group = linkedin(found, "a", target="ai_answers")["success"]
    assert group["ranked_of"] == (4 if counts else 3)


def test_a_run_cut_short_moves_no_overall_and_no_rank():
    without = full(LINKEDIN)
    # Shifter's LinkedIn hour (#1134) cut short at 99: 11% ok, weight 5. Another: 60% ok.
    for run in ((0.11, 99), (0.60, 99), (0.11, 1)):
        found = full(LINKEDIN, run)
        assert {s["status"] for s in linkedin(found, "d").values()} == {ts.NOT_ENOUGH_DATA}
        assert linkedin(found, "d")["success"]["score"] is None
        for name in HTTP:
            for key in ("success_overall", "challenge_overall"):
                assert overall(found, name, key) == overall(without, name, key), (run, name)
        for name in LINKEDIN:
            assert linkedin(found, name) == linkedin(without, name)
            assert linkedin(found, name)["success"]["ranked_of"] == 3
    # d's overall counts generic_http alone (weights renormalised over the counted targets).
    d = overall(full(LINKEDIN, (0.11, 99)), "d")
    assert d["targets"] == {"generic_http": 1} and d["weights"] == {"generic_http": 1}
    a = overall(full(LINKEDIN, (0.11, 99)), "a")
    assert set(a["targets"]) == set(a["weights"]) == {"generic_http", "linkedin_public"}


def test_one_run_of_100_counts_in_the_overall_and_the_ranking():
    low, found = overall(full(LINKEDIN, (0.60, 99)), "d"), full(LINKEDIN, (0.60, 100))
    assert linkedin(found, "d")["success"] == {
        "rate": 0.60,
        "score": 10,
        "rank": 1,
        "ranked_of": 4,
        "status": ts.RANKED,
    }
    # d has the most challenges on generic_http (1) and the fewest on LinkedIn (10, weight 5):
    # its overall moves.
    d = overall(found, "d")
    assert d["weights"] == {"generic_http": 1, "linkedin_public": 5}
    assert low["score"] == 1 and d["score"] == pytest.approx((1 + 10 * 5) / 6)
    assert d["rank"] < low["rank"]
    assert linkedin(found, "c")["success"]["ranked_of"] == 4


def test_a_network_under_the_minimum_is_no_peer_for_the_three_ranked():
    found = full({"a": 0.5, "b": 0.4}, (0.3, 99))
    statuses = {name: linkedin(found, name)["success"]["status"] for name in ("a", "b", "d")}
    few, none = ts.TOO_FEW_NETWORKS, ts.NOT_ENOUGH_DATA
    assert statuses == {"a": few, "b": few, "d": none}
    assert set(overall(found, "a")["targets"]) == {"generic_http"}


def test_each_window_counts_its_own_tries():
    rows = {name: {w: (rate, 600) for w in WINDOWS} for name, rate in LINKEDIN.items()}
    found = networks(rows | {"d": {"24h": (0.11, 99), "7d": (0.11, 100), "30d": (0.11, 101)}})
    assert linkedin(found, "d", "24h")["success"]["status"] == ts.NOT_ENOUGH_DATA
    assert "linkedin_public" not in overall(found, "d")["weights"]
    for label in ("7d", "30d"):
        assert linkedin(found, "d", label)["success"]["status"] == ts.RANKED
        assert "linkedin_public" in overall(found, "d", label=label)["weights"]


def test_the_http_targets_keep_100_and_the_browser_overall_follows():
    found = full(LINKEDIN, (0.11, 99))
    assert overall(found, "d", "browser_overall")["camoufox"]["success"]["status"] == (
        ts.NO_RANKED_TARGETS
    )
    assert overall(found, "a", "browser_overall")["camoufox"]["targets"] == ["linkedin_public"]
    assert [s["score"] for s in http_scores(150, 150, 150)] == [10, 5.5, 1]
    assert [s["score"] for s in http_scores(150, 100, 100)] == [10, 5.5, 1]
    statuses = [s["status"] for s in http_scores(150, 100, 99)]
    assert statuses == [ts.TOO_FEW_NETWORKS] * 2 + [ts.NOT_ENOUGH_DATA]


def http_scores(*tries):
    """generic_http's success scores of three networks at 90%, 50% and 10% with these tries."""
    peers = [
        {"kind": "k", "windows": {"24h": {"targets": {"generic_http": http(r, n)}}}}
        for r, n in zip((0.9, 0.5, 0.1), tries, strict=True)
    ]
    ts.score_networks(peers, CONFIG)
    return [p["windows"]["24h"]["targets"]["generic_http"]["scores"]["success"] for p in peers]


def test_a_day_leaves_a_sub_minimum_browser_target_out():
    found = []
    for name, rate in HTTP.items():
        targets = {"generic_http": http(rate)}
        tries = 99 if name == "d" else 100
        targets["linkedin_public"] = browser(LINKEDIN.get(name, 0.11), tries)
        found.append({"kind": "residential", "days": [{"start": "2026-11-14", "targets": targets}]})
    ts.score_days(found, CONFIG)
    days = [n["days"][0]["success_overall"]["targets"] for n in found]
    assert [set(t) for t in days] == [{"generic_http", "linkedin_public"}] * 3 + [{"generic_http"}]
