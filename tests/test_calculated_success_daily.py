"""SUCCESS-DAILY-DISTANCE-1 (#1229, the owner's decision of 2026-11-16): a network window's
success_overall is its daily tests' success by requests on generic_http, one pooled ratio, scored
by its distance from the best network of the kind (target_scores.daily_counts() and
daily_overall(); the methodology's [success_score])."""

from pathlib import Path

import pytest

from atlas.calculated import target_scores as ts
from atlas.calculated.policy import SUCCESS_SCORE, load, success_score, validate

CONFIG = load(Path("configs/methodology.toml"))[0]
# The owner's table (08:06 build window, 24h): each network's daily-test success by requests,
# in percent, and the score it gives (#1229).
OWNER = {
    "geonode": (99.30, 10.00),
    "evomi": (99.23, 9.93),
    "decodo": (99.08, 9.78),
    "shifter": (97.97, 8.67),
    "massive": (96.98, 7.68),
    "soax": (91.17, 1.87),
    "proxy-cheap": (84.89, 1.00),
    "rayobyte": (80.43, 1.00),
}


def row(completed, successes, test=None, target="generic_http"):
    """A network bucket row: its run's batch kind (None: a daily test) and its counters."""
    counters = {"completed": completed, "successes": successes}
    return {"test": test, "target": target, "counters": counters}


def windows(*daily):
    """One window per network, each with its "_daily" {target: [completed, successes]}."""
    return [{"targets": {}, "_daily": d} for d in daily]


def at(percent, completed=100_000):
    return {"generic_http": [completed, round(completed * percent / 100)]}


def test_the_daily_tests_requests_are_one_pooled_ratio():
    rows = [row(600, 590), row(400, 390), row(10, 0, "pulse"), row(10, 0, "load")]
    rows += [row(10, 0, "census"), row(10, 0, "browser"), row(10, 0, target="generic_https")]
    # Pooled: (590 + 390) / (600 + 400) = 98%, not the mean of 98.3% and 97.5%.
    assert ts.daily_counts(rows) == {"generic_http": [1000, 980]}
    assert ts.daily_counts([row(10, 5, "pulse")]) == {}
    assert ts.DAILY_TARGETS == ("generic_http",)


def test_the_owner_example_scores_by_distance_from_the_leader():
    found = windows(*(at(rate) for rate, _ in OWNER.values()))
    ts.daily_overall(found, SUCCESS_SCORE)
    scores = [w["success_overall"]["score"] for w in found]
    assert scores == pytest.approx([score for _, score in OWNER.values()], abs=0.005)
    shifter = found[3]["success_overall"]
    assert shifter["targets"] == {"generic_http": pytest.approx(8.67)}
    assert shifter["weights"] == {"generic_http": 1}
    assert (shifter["rank"], shifter["ranked_of"], shifter["status"]) == (4, 8, ts.RANKED)
    assert list(shifter) == ["score", "rank", "ranked_of", "status", "targets", "weights"]
    assert "_daily" not in found[3]


def test_the_leader_reads_ten_and_the_floor_holds_ties_share_their_place():
    found = windows(at(99.0), at(97.5), at(97.5), at(80.0), at(60.0))
    ts.daily_overall(found, SUCCESS_SCORE)
    overall = [w["success_overall"] for w in found]
    assert overall[0]["score"] == 10
    assert overall[1]["score"] == overall[2]["score"] == pytest.approx(8.5)
    assert [o["score"] for o in overall[3:]] == [1.0, 1.0]
    assert [o["rank"] for o in overall] == [1, 2.5, 2.5, 4.5, 4.5]


def test_too_few_requests_or_no_counting_daily_test_has_no_score():
    # 99 requests at 100% have no value and lead nobody; a network with no daily test that
    # counted (the build reads no row of a run whose country did not count) has none.
    found = windows(at(98.0), at(100.0, ts.MIN_REQUESTS - 1), {}, at(97.0, ts.MIN_REQUESTS))
    found.append({"targets": {}})
    ts.daily_overall(found, SUCCESS_SCORE)
    overall = [w["success_overall"] for w in found]
    assert overall[0]["score"] == 10 and overall[3]["score"] == pytest.approx(9)
    for o in (overall[1], overall[2], overall[4]):
        assert o == {
            "score": None,
            "rank": None,
            "ranked_of": None,
            "status": ts.NO_RANKED_TARGETS,
            "targets": {},
            "weights": {},
        }
    assert [o["ranked_of"] for o in overall] == [2, None, None, 2, None]


def test_the_slope_and_floor_are_the_methodologys():
    assert success_score(CONFIG) == {"basis": "daily_tests", "slope": 1.0, "floor": 1.0}
    assert success_score({}) == SUCCESS_SCORE
    found = windows(at(99.0), at(98.0), at(90.0))
    ts.daily_overall(found, {"basis": "daily_tests", "slope": 2.0, "floor": 3.0})
    assert [w["success_overall"]["score"] for w in found] == pytest.approx([10, 8, 3])


@pytest.mark.parametrize(
    "table",
    [
        {"basis": "pulses", "slope": 1.0, "floor": 1.0},
        {"basis": "daily_tests", "slope": 0, "floor": 1.0},
        {"basis": "daily_tests", "slope": 1.0, "floor": 10},
        {"basis": "daily_tests", "slope": True, "floor": 1.0},
        {"basis": "daily_tests", "slope": 1.0},
        "daily_tests",
    ],
)
def test_the_methodology_refuses_an_invalid_success_score(table):
    with pytest.raises(ValueError, match="Invalid success_score"):
        validate(CONFIG | {"success_score": table})


def test_the_networks_are_scored_per_kind_and_window():
    def entry(kind, rate):
        return {"kind": kind, "windows": {"24h": windows(at(rate))[0], "7d": windows({})[0]}}

    found = [entry("residential", 99.0), entry("residential", 97.0), entry("mobile", 90.0)]
    ts.score_networks(found, CONFIG)
    day = [n["windows"]["24h"]["success_overall"] for n in found]
    assert [o["score"] for o in day] == pytest.approx([10, 8, 10])
    assert [o["ranked_of"] for o in day] == [2, 2, 1]
    week = {n["windows"]["7d"]["success_overall"]["status"] for n in found}
    assert week == {ts.NO_RANKED_TARGETS}
