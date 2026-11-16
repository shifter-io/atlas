"""SCORE-BROWSER-MIN-1 (#1134, the owner's decision of 2026-11-15): a browser target counts in a
network's overall scores only with the methodology's browser_min_requests (500) completed tries
of its engine in the window. Under it the target is not_enough_data, left out of the overalls
and of the target's ranking, so one browser run of a network moves no overall and no rank. The
success overall reads the daily tests alone (SUCCESS-DAILY-DISTANCE-1), so the overall the
browser targets count in is challenge_overall."""

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


def browser(ok, completed):
    """A browser target's entry with Camoufox figures only."""
    own = {"completed": completed, "successes": round(ok * completed)}
    own["outcome_rates"] = {"ok": ok, "challenge": 1 - ok, "block": 0.0}
    lead = {"engine": "camoufox", "outcome_rates": own["outcome_rates"]}
    return {"metrics": {}, "browser": lead | {"engines": {"camoufox": own}}}


def http(rate, completed=10_000):
    metrics = {"completed": completed, "success_rate": rate}
    return {"metrics": metrics | {"challenge_rate": 1 - rate, "block_rate": 0.0}}


def networks(linkedin):
    """Four residential networks a to d: generic_http measured well in each window, and
    linkedin_public where linkedin[name] = {label: (ok, completed)} has it."""
    out = []
    for name, rate in HTTP.items():
        windows = {}
        for label in ("24h", "7d"):
            windows[label] = {"targets": {"generic_http": http(rate)}}
            if label in linkedin.get(name, {}):
                windows[label]["targets"]["linkedin_public"] = browser(*linkedin[name][label])
        out.append({"id": name, "kind": "residential", "windows": windows})
    ts.score_networks(out, CONFIG)
    return {n["id"]: n["windows"] for n in out}


def full(rates, d=None):
    """networks() with a to c at 600 tries of linkedin_public and d at `d` (ok, completed)."""
    found = {name: {"24h": (rate, 600)} for name, rate in rates.items()}
    return networks(found | ({"d": {"24h": d}} if d else {}))


def overall(found, name, key="challenge_overall", label="24h"):
    return found[name][label][key]


def linkedin(found, name, label="24h"):
    return found[name][label]["targets"]["linkedin_public"]["browser"]["scores"]


def test_the_owner_set_500_and_the_default_is_the_http_minimum():
    assert CONFIG["browser_min_requests"] == 500 == browser_min_requests(CONFIG)
    assert BROWSER_MIN_REQUESTS == ts.MIN_REQUESTS == 100
    assert browser_min_requests({}) == ts.MIN_REQUESTS


@pytest.mark.parametrize("value", [0, -1, 2.5, True, "500", None])
def test_the_methodology_refuses_an_invalid_minimum(value):
    with pytest.raises(ValueError, match="Invalid browser_min_requests"):
        validate(CONFIG | {"browser_min_requests": value})


def test_one_run_of_100_moves_no_overall_and_no_rank():
    without = full(LINKEDIN)
    # Shifter's LinkedIn hour (#1134): 100 requests, 11 ok, weight 5. Another hour: 100 of 60%.
    for run in ((0.11, 100), (0.60, 100), (0.11, 499)):
        found = full(LINKEDIN, run)
        assert {s["status"] for s in linkedin(found, "d").values()} == {ts.NOT_ENOUGH_DATA}
        assert linkedin(found, "d")["success"]["score"] is None
        for name in HTTP:
            for key in ("success_overall", "challenge_overall"):
                assert overall(found, name, key) == overall(without, name, key), (run, name)
        for name in LINKEDIN:
            assert linkedin(found, name) == linkedin(without, name)
            assert linkedin(found, name)["success"]["ranked_of"] == 3
    # d's overall counts generic_http alone (weights renormalised over the counted targets): its
    # challenge score is the worst of four, a 1.
    d = overall(full(LINKEDIN, (0.11, 100)), "d")
    assert d["targets"] == {"generic_http": 1} and d["weights"] == {"generic_http": 1}
    a = overall(full(LINKEDIN, (0.11, 100)), "a")
    assert set(a["targets"]) == set(a["weights"]) == {"generic_http", "linkedin_public"}


def test_500_tries_count_in_the_overall_and_the_ranking():
    low, found = overall(full(LINKEDIN, (0.60, 100)), "d"), full(LINKEDIN, (0.60, 500))
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
    found = full({"a": 0.5, "b": 0.4}, (0.3, 400))
    statuses = {name: linkedin(found, name)["success"]["status"] for name in ("a", "b", "d")}
    few, none = ts.TOO_FEW_NETWORKS, ts.NOT_ENOUGH_DATA
    assert statuses == {"a": few, "b": few, "d": none}
    assert set(overall(found, "a")["targets"]) == {"generic_http"}


def test_each_window_counts_its_own_tries():
    rows = {name: {"24h": (rate, 600), "7d": (rate, 3000)} for name, rate in LINKEDIN.items()}
    found = networks(rows | {"d": {"24h": (0.11, 400), "7d": (0.11, 2000)}})
    assert linkedin(found, "d", "24h")["success"]["status"] == ts.NOT_ENOUGH_DATA
    assert "linkedin_public" not in overall(found, "d")["weights"]
    assert linkedin(found, "d", "7d")["success"]["status"] == ts.RANKED
    assert "linkedin_public" in overall(found, "d", label="7d")["weights"]


def test_the_http_targets_keep_100_and_the_browser_overall_follows():
    found = full(LINKEDIN, (0.11, 100))
    assert overall(found, "d", "browser_overall")["camoufox"]["success"]["status"] == (
        ts.NO_RANKED_TARGETS
    )
    assert overall(found, "a", "browser_overall")["camoufox"]["targets"] == ["linkedin_public"]
    peers = [
        {"kind": "k", "windows": {"24h": {"targets": {"generic_http": http(r, 150)}}}}
        for r in (0.9, 0.5, 0.1)
    ]
    ts.score_networks(peers, CONFIG)
    scores = [p["windows"]["24h"]["targets"]["generic_http"]["scores"]["success"] for p in peers]
    assert [s["score"] for s in scores] == [10, 5.5, 1]


def test_a_day_counts_no_browser_target():
    found = []
    for name, rate in HTTP.items():
        targets = {"generic_http": http(rate)}
        tries = 100 if name == "d" else 600
        targets["linkedin_public"] = browser(LINKEDIN.get(name, 0.11), tries)
        day = {"start": "2026-11-14", "targets": targets, "_daily": {"generic_http": [500, 450]}}
        found.append({"kind": "residential", "days": [day]})
    ts.score_days(found, CONFIG)
    days = [n["days"][0]["success_overall"]["targets"] for n in found]
    assert days == [{"generic_http": 10}] * 4
