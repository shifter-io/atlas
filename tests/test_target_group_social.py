"""TARGET-GROUP-SOCIAL-1 (#1231, the owner, 2026-11-16): social_web, "Social media web app", is a
group target of instagram_web and x_web wired as ai_answers is: measured over the union of its
sources' tries and never scheduled, counted once in an overall (never its sources), and ranked on
that union with the methodology's browser_min_requests (MIN: the tests scale with it)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from calculated_browser_cases import row

from atlas.browser import contracts
from atlas.calculated import browser
from atlas.calculated import target_scores as ts
from atlas.calculated.policy import browser_difficulty, browser_min_requests, load
from atlas.calculated.value_all import card_targets
from atlas.regions import load_targets

CONFIG, _ = load(Path("configs/methodology.toml"))
GROUP, SOURCES = contracts.SOCIAL_WEB, list(contracts.SOCIAL_SOURCES)
SINCE = datetime(2026, 11, 16, tzinfo=UTC)
END = SINCE + timedelta(hours=2)
MIN = browser_min_requests(CONFIG)


def test_the_methodology_groups_instagram_and_x_as_one_social_target():
    assert CONFIG["target_groups"][GROUP] == SOURCES == ["instagram_web", "x_web"]
    assert GROUP not in contracts.RUN_TARGETS and set(SOURCES) <= set(contracts.RUN_TARGETS)
    assert MIN >= 2  # the methodology's own minimum, unchanged here
    assert browser_difficulty(CONFIG)[GROUP] == 3
    labels = {r["target_id"]: r["label"] for r in load_targets()}
    assert [labels[t] for t in (GROUP, *SOURCES)] == [
        "Social media web app",
        "Instagram",
        "X public profile",
    ]
    cards = card_targets(CONFIG)
    assert GROUP in cards and not set(SOURCES) & set(cards)


def measured(rows):
    """browser.targets() of a window's browser rows: {target: (its rows, its browser object)}."""
    found = browser.targets(CONFIG, [], rows, SINCE, END)
    return {target: (own, extra["browser"]) for target, own, extra in found if extra}


def test_the_group_is_measured_over_the_union_of_its_sources():
    rows = [
        row(SINCE, 100, 90, target="instagram_web"),
        row(SINCE + timedelta(hours=1), 100, 50, target="x_web"),
        row(SINCE, 100, 100, target="amazon_pdp"),  # no source: not in the group
    ]
    found = measured(rows)
    assert list(found) == ["amazon_pdp", GROUP, "instagram_web", "x_web"]
    own, group = found[GROUP]
    assert own == rows[:2]
    assert group["outcome_rates"]["ok"] == 140 / 200
    assert group["engines"]["camoufox"]["completed"] == 200
    assert (group["group"], group["sources"]) == (None, SOURCES)
    for source in SOURCES:
        assert (found[source][1]["group"], found[source][1]["sources"]) == (GROUP, None)


def test_a_network_with_one_sources_tries_has_the_group_over_those():
    rows = [row(SINCE, 100, 70, target="x_web")]
    found = measured(rows)
    assert list(found) == [GROUP, "x_web"]
    assert found[GROUP][0] == rows and found[GROUP][1]["outcome_rates"]["ok"] == 0.7
    assert found[GROUP][1]["sources"] == SOURCES  # both named, one measured
    assert measured([]) == {}  # no source tries: no group entry


def engine(ok, completed):
    rates = {"ok": ok, "challenge": 0.0, "block": 1 - ok}
    return {"completed": completed, "successes": round(ok * completed), "outcome_rates": rates}


def network(provider, ok, instagram, x):
    """A residential network's 24h window with Camoufox figures of social_web and its sources:
    `instagram` and `x` completed tries, the group the union of both."""
    targets = {}
    for target, n in ((GROUP, instagram + x), ("instagram_web", instagram), ("x_web", x)):
        own = engine(ok, n)
        main = {"engine": "camoufox", "outcome_rates": own["outcome_rates"]}
        targets[target] = {"metrics": {}, "browser": main | {"engines": {"camoufox": own}}}
    return {"provider": provider, "kind": "residential", "windows": {"24h": {"targets": targets}}}


def scored(networks, target, metric="success"):
    return [n["windows"]["24h"]["targets"][target]["browser"]["scores"][metric] for n in networks]


def test_the_minimum_counts_the_union_and_the_group_is_ranked_and_counted_once():
    # MIN - 1 tries of each source: neither reaches the minimum alone, their union does.
    n = MIN - 1
    found = [network(p, ok, n, n) for p, ok in (("a", 0.9), ("b", 0.6), ("c", 0.3))]
    ts.score_networks(found, CONFIG)
    group = scored(found, GROUP)
    assert [s["status"] for s in group] == [ts.RANKED] * 3
    assert [s["rank"] for s in group] == [1, 2, 3] and [s["score"] for s in group] == [10, 5.5, 1]
    assert [s["rank"] for s in scored(found, GROUP, "block")] == [1, 2, 3]
    for source in SOURCES:
        assert {s["status"] for s in scored(found, source)} == {ts.NOT_ENOUGH_DATA}
    for entry, score in zip(found, (10, 5.5, 1), strict=True):
        w = entry["windows"]["24h"]
        overall = w["browser_overall"]["camoufox"]
        assert overall["targets"] == [GROUP] and overall["weights"] == {GROUP: 3}
        assert overall["success"]["score"] == score
        assert set(w["challenge_overall"]["weights"]) == {GROUP}


def test_a_group_under_the_minimum_is_not_enough_data_and_ranked_sources_never_count():
    # Under the minimum on the union too.
    low, high = (MIN - 1) // 2, MIN - 1 - (MIN - 1) // 2
    short = [network(p, ok, low, high) for p, ok in (("a", 0.9), ("b", 0.6), ("c", 0.3))]
    ts.score_networks(short, CONFIG)
    assert {s["status"] for s in scored(short, GROUP)} == {ts.NOT_ENOUGH_DATA}
    assert short[0]["windows"]["24h"]["browser_overall"]["camoufox"]["targets"] == []
    # The minimum on each source: the sources are ranked, yet only the group counts.
    full = [network(p, ok, MIN, MIN) for p, ok in (("a", 0.9), ("b", 0.6), ("c", 0.3))]
    ts.score_networks(full, CONFIG)
    assert {s["status"] for s in scored(full, "x_web")} == {ts.RANKED}
    assert full[0]["windows"]["24h"]["browser_overall"]["camoufox"]["targets"] == [GROUP]
