"""SUCCESS-ALL-8-1 (the owner's decision of 2026-11-19): a network window's success_overall is the
mean of its 8 card targets' distance scores weighted by their fixed difficulty, shifted so the best
network of the kind reads 10 (target_scores.success_overall(); the methodology's [success_score]).
Which tries a window counts: tests/test_card_success.py."""

from pathlib import Path

import pytest

from atlas.calculated import card_success
from atlas.calculated import target_scores as ts
from atlas.calculated.policy import SUCCESS_SCORE, load, success_score, validate

CONFIG = load(Path("configs/methodology.toml"))[0]
WEIGHTS = card_success.weights(CONFIG)
CARDS = list(WEIGHTS)
TRIES = 1000


def window(base, completed=TRIES, **rates):
    """A window whose card targets succeed at base percent, those named in rates at theirs (None:
    no try at all), each with `completed` tries."""
    found = {}
    for t in CARDS:
        rate = rates.get(t, base)
        if rate is not None:
            found[t] = [completed, round(completed * rate / 100)]
    return {"targets": {}, "_success": found}


def overall(*windows, settings=SUCCESS_SCORE):
    ts.success_overall(list(windows), settings, WEIGHTS)
    return [w["success_overall"] for w in windows]


def test_the_card_targets_weigh_their_fixed_difficulty():
    assert WEIGHTS == {
        "generic_http": 1,
        "generic_https": 1,
        "amazon_pdp": 2,
        "social_web": 3,
        "google_search": 4,
        "cloudflare_challenge": 4,
        "linkedin_public": 5,
        "ai_answers": 5,
    }
    shares = [w / sum(WEIGHTS.values()) for w in WEIGHTS.values()]
    assert shares == pytest.approx([0.04, 0.04, 0.08, 0.12, 0.16, 0.16, 0.20, 0.20])


def test_each_target_scores_its_distance_and_the_mean_is_difficulty_weighted():
    """a leads every target but Google (40% against b's 45%: 5 behind, a 5); b is 3 points behind
    everywhere else (a 7). Raw: a (21 x 10 + 4 x 5) / 25 = 9.2, b (21 x 7 + 4 x 10) / 25 = 7.48;
    shifted by +0.8 so a reads 10: b 8.28."""
    a, b = overall(window(95.0, google_search=40.0), window(92.0, google_search=45.0))
    assert a["targets"] == {t: 5.0 if t == "google_search" else 10.0 for t in CARDS}
    assert b["targets"]["google_search"] == 10 and b["targets"]["generic_http"] == pytest.approx(7)
    assert a["weights"] == b["weights"] == WEIGHTS
    assert a["score"] == 10 and b["score"] == pytest.approx(8.28)
    assert (a["rank"], b["rank"], a["ranked_of"]) == (1, 2, 2)
    assert a["status"] == b["status"] == ts.RANKED
    assert a["missing_targets"] == [] and a["basis"] == "card_targets"
    keys = ["score", "rank", "ranked_of", "status", "targets", "weights", "missing_targets"]
    assert list(a) == [*keys, "basis"]


def test_a_hard_target_weighs_five_times_plain_web():
    """The same 5-point gap costs 5 x 1 / 25 = 0.2 on plain web and 5 x 5 / 25 = 1.0 on LinkedIn."""
    best, http, linkedin = overall(
        window(90.0), window(90.0, generic_http=85.0), window(90.0, linkedin_public=85.0)
    )
    assert best["score"] == 10
    assert http["score"] == pytest.approx(9.8) and linkedin["score"] == pytest.approx(9.0)


def test_a_target_without_a_try_is_left_out_and_named():
    full, partial = overall(window(90.0), window(90.0, linkedin_public=None, ai_answers=None))
    assert partial["status"] == ts.PARTIAL_TARGETS
    assert partial["missing_targets"] == ["linkedin_public", "ai_answers"]
    assert set(partial["targets"]) == set(partial["weights"]) == set(CARDS) - {
        "linkedin_public",
        "ai_answers",
    }
    # The weights renormalised over the 6 targets it has: all at the best, it reads 10 too.
    assert partial["score"] == full["score"] == 10
    assert (partial["rank"], partial["ranked_of"]) == (1.5, 2)


def test_a_target_with_tries_and_no_success_counts_as_zero():
    """IPRoyal's LinkedIn of 19 Nov: 300 tries, no success. Against the best 14% it is 14 points
    behind, the floor (1), and it is not missing."""
    best, none = overall(
        window(90.0, linkedin_public=14.0), window(90.0, completed=300, linkedin_public=0.0)
    )
    assert none["status"] == ts.RANKED and none["missing_targets"] == []
    assert none["targets"]["linkedin_public"] == 1
    assert none["score"] == pytest.approx(10 - 9 * 5 / 25)


def test_no_try_on_any_target_has_no_score():
    found = overall(window(90.0), window(80.0), {"targets": {}, "_success": {}}, {"targets": {}})
    assert found[2] == found[3] == {
        "score": None,
        "rank": None,
        "ranked_of": None,
        "status": ts.NO_RANKED_TARGETS,
        "targets": {},
        "weights": {},
        "missing_targets": CARDS,
        "basis": "card_targets",
    }
    assert [o["ranked_of"] for o in found] == [2, 2, None, None]


def test_ties_share_their_place_and_the_floor_holds():
    found = overall(window(99.0), window(97.5), window(97.5), window(80.0), window(60.0))
    assert found[0]["score"] == 10
    assert found[1]["score"] == found[2]["score"] == pytest.approx(8.5)
    assert [o["score"] for o in found[3:]] == [1.0, 1.0]
    assert [o["rank"] for o in found] == [1, 2.5, 2.5, 4.5, 4.5]


def test_the_best_reads_ten_after_the_shift_and_gaps_stay():
    """No network leads everywhere: the raw best is under 10 and every score moves by the same
    offset, so the best reads exactly 10 and the gaps are the measured ones. Raw: a (21 x 10 +
    4 x 6) / 25 = 9.36, b (21 x 9.5 + 4 x 10) / 25 = 9.58, c (21 x 5 + 4 x 5) / 25 = 5; +0.42."""
    a, b, c = overall(
        window(95.0, google_search=36.0),
        window(94.5, google_search=40.0),
        window(90.0, google_search=35.0),
    )
    assert b["score"] == 10
    assert a["score"] == pytest.approx(10 - (9.58 - 9.36)) and c["score"] == pytest.approx(5.42)
    assert b["targets"]["generic_http"] == pytest.approx(9.5)  # the target scores are not shifted
    assert [o["rank"] for o in (a, b, c)] == [2, 1, 3]


def test_the_slope_and_floor_are_the_methodologys():
    assert success_score(CONFIG) == {"basis": "card_targets", "slope": 1.0, "floor": 1.0}
    assert success_score({}) == SUCCESS_SCORE
    settings = {"basis": "card_targets", "slope": 2.0, "floor": 3.0}
    found = overall(window(99.0), window(98.0), window(90.0), settings=settings)
    assert [o["score"] for o in found] == pytest.approx([10, 8, 3])


@pytest.mark.parametrize(
    "table",
    [
        {"basis": "daily_tests", "slope": 1.0, "floor": 1.0},
        {"basis": "card_targets", "slope": 0, "floor": 1.0},
        {"basis": "card_targets", "slope": 1.0, "floor": 10},
        {"basis": "card_targets", "slope": True, "floor": 1.0},
        {"basis": "card_targets", "slope": 1.0},
        "card_targets",
    ],
)
def test_the_methodology_refuses_an_invalid_success_score(table):
    with pytest.raises(ValueError, match="Invalid success_score"):
        validate(CONFIG | {"success_score": table})


def test_the_networks_are_scored_per_kind_and_window():
    def entry(kind, rate):
        return {"kind": kind, "windows": {"24h": window(rate), "7d": window(0, generic_http=None)}}

    found = [entry("residential", 99.0), entry("residential", 97.0), entry("mobile", 90.0)]
    for n in found:
        n["windows"]["7d"]["_success"] = {}
    ts.score_networks(found, CONFIG)
    day = [n["windows"]["24h"]["success_overall"] for n in found]
    assert [o["score"] for o in day] == pytest.approx([10, 8, 10])
    assert [o["ranked_of"] for o in day] == [2, 2, 1]
    week = {n["windows"]["7d"]["success_overall"]["status"] for n in found}
    assert week == {ts.NO_RANKED_TARGETS}
    assert all("_success" not in w for n in found for w in n["windows"].values())
