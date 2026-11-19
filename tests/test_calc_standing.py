"""STANDING-FREEZE-1: a network that is no longer measured keeps its published standing frozen at
its last values, and never ranks higher on stale data (calculated/standing.py). Unit cases over
hand-built network windows; test_calc_standing_db.py runs the rule through published builds."""

import copy
from datetime import UTC, datetime, timedelta

import pytest

from atlas.calculated import standing
from atlas.calculated.policy import STANDING, standing_settings

AS_OF = datetime(2026, 11, 19, 18, tzinfo=UTC)
HOUR = timedelta(hours=1)
CONFIG = {"standing": dict(STANDING)}
CURRENT = {
    "pool": {"unique_ips": 7},
    "lists": {"id": 99, "entries": {}},
    "breakdown_entries": {},
    "overlap": None,
}


def window(last, hours=24, label_hours=24, score=None):
    """A window as summaries() builds it: last its newest request, hours those with one."""
    return {
        "period": {"start": AS_OF - label_hours * HOUR, "end": AS_OF, "hours": label_hours},
        "metrics": {"last_observed": last, "completed": 500, "successes": 400, "errors": 100},
        "series": [],
        "scores": {"balanced": score} if score else {},
        "_hours": (hours, label_hours),
    }


def entry(last, hours=24, network="a:residential:network"):
    windows = {"24h": window(last, hours), "30d": window(last, 700, 720)}
    return {"id": network, "kind": "residential", "windows": windows, "days": ["today"]}


def stored(status=None, since=None):
    """A stored network entry (JSON times), its windows' standing status when given."""
    found = {}
    for label, hours in (("24h", 24), ("30d", 720)):
        w = {
            "period": {"start": "2026-11-18T13:00:00+00:00", "end": "2026-11-19T13:00:00+00:00"},
            "metrics": {"last_observed": "2026-11-19T12:50:00+00:00", "completed": 900},
            "series": [{"at": "2026-11-19T12:00:00+00:00", "success_score": {"score": 10}}],
            "scores": {"balanced": {"score": 90.0, "rank": 1, "state": "ready"}},
            "success_overall": {"score": 10.0, "rank": 1.0, "ranked_of": 4, "status": "ranked"},
            "pool": {"unique_ips": 500},
            "hours": hours,
        }
        if status:
            w["standing"] = {"status": status, "last_measured": w["metrics"]["last_observed"]}
            w["standing"] |= {"frozen_since": since, "values_from": "2026-11-19T13:00:00+00:00"}
        found[label] = w
    return {"id": "a:residential:network", "windows": found, "days": ["frozen day"]}


def test_settings_default_and_validation():
    assert standing_settings({}) == {"freeze_after_hours": 3, "lift_coverage": 0.9}
    assert standing_settings({"standing": {"freeze_after_hours": 0}})["freeze_after_hours"] == 0
    for bad in ({"freeze_after_hours": 1.5}, {"lift_coverage": 2}, {"other": 1}, []):
        with pytest.raises(ValueError, match="standing"):
            standing_settings({"standing": bad})


def test_one_missed_hour_does_not_freeze_three_hours_do():
    """Pulses are hourly: a newest request 2 h 59 min old is a missed hour, measured; at 3 h every
    window is held, frozen at the newest build in which it was still measured."""
    measured = [entry(AS_OF - 3 * HOUR + timedelta(minutes=1))]
    standing.mark(measured, {}, AS_OF, CONFIG)
    w = measured[0]["windows"]["24h"]
    assert w["standing"]["status"] == standing.MEASURED and "_held" not in w and "_hours" not in w
    assert w["standing"]["last_measured"] == AS_OF - 3 * HOUR + timedelta(minutes=1)
    source = {"held": None, "measured": {"as_of": AS_OF - 5 * HOUR, "entry": stored()}}
    stopped = [entry(AS_OF - 3 * HOUR)]
    held = standing.mark(stopped, {stopped[0]["id"]: source}, AS_OF, CONFIG)
    assert held == {(stopped[0]["id"], "24h"), (stopped[0]["id"], "30d")}
    for label, w in stopped[0]["windows"].items():
        assert w["_held"] is source["measured"]["entry"]["windows"][label]
        assert w["standing"] == {
            "status": standing.FROZEN,
            "last_measured": AS_OF - 3 * HOUR,
            "frozen_since": AS_OF,
            "values_from": AS_OF - 5 * HOUR,
        }
    assert stopped[0]["days"] == ["frozen day"]


def test_without_earlier_values_a_stopped_network_has_no_standing():
    networks = [entry(AS_OF - 10 * HOUR), entry(None, 0, "b:residential:network")]
    standing.mark(networks, {}, AS_OF, CONFIG)
    for n in networks:
        for w in n["windows"].values():
            assert w["_held"] is None and w["standing"]["status"] == standing.STOPPED
        assert n["days"] == []
    assert standing.live(networks)[0]["windows"] == {}


def test_a_held_window_stays_held_until_measured_and_covered_again():
    """Measured again for two hours: the 24h window (2 of 24 hours) stays frozen with its first
    freeze's values and time; the 30d window (700 of 720 hours) lifts. With 22 hours it lifts."""
    since = "2026-11-19T16:00:00+00:00"
    found = {"a:residential:network": {"held": stored(standing.FROZEN, since), "measured": None}}
    networks = [entry(AS_OF - HOUR, hours=2)]
    standing.mark(networks, found, AS_OF, CONFIG)
    day, month = networks[0]["windows"]["24h"], networks[0]["windows"]["30d"]
    assert day["_held"] is found["a:residential:network"]["held"]["windows"]["24h"]
    assert day["standing"]["frozen_since"] == since
    assert day["standing"]["last_measured"] == AS_OF - HOUR
    assert month["standing"]["status"] == standing.MEASURED and "_held" not in month
    assert networks[0]["days"] == ["today"]
    covered = [entry(AS_OF - HOUR, hours=22)]
    standing.mark(covered, found, AS_OF, CONFIG)
    assert all(w["standing"]["status"] == standing.MEASURED for w in covered[0]["windows"].values())


def test_a_stopped_window_stays_stopped():
    found = {"a:residential:network": {"held": stored(standing.STOPPED), "measured": None}}
    networks = [entry(AS_OF - 8 * HOUR)]
    standing.mark(networks, found, AS_OF, CONFIG)
    assert all(w["_held"] is None for w in networks[0]["windows"].values())
    assert networks[0]["windows"]["24h"]["standing"]["status"] == standing.STOPPED


def test_zero_hours_turns_the_freeze_off():
    networks = [entry(AS_OF - 100 * HOUR, hours=0)]
    found = {"a:residential:network": {"held": stored(standing.FROZEN), "measured": None}}
    standing.mark(networks, found, AS_OF, {"standing": {"freeze_after_hours": 0}})
    assert all(
        w["standing"]["status"] == standing.MEASURED for w in networks[0]["windows"].values()
    )


def held_network(kept, status=standing.FROZEN):
    current = window(AS_OF - 5 * HOUR) | copy.deepcopy(CURRENT) | {"_lead": {}}
    del current["_hours"]
    current["_held"] = kept
    current["standing"] = {"status": status, "last_measured": AS_OF - 5 * HOUR}
    return {"id": "a:residential:network", "kind": "residential", "windows": {"24h": current}}


def test_a_frozen_window_keeps_its_values_and_the_current_pool():
    kept = stored()["windows"]["24h"]
    networks = [held_network(kept)]
    previous = {"networks": [{"id": "a:residential:network", "windows": {"24h": {"scores": {}}}}]}
    previous["networks"][0]["windows"]["24h"]["scores"]["balanced"] = {"rank": 3}
    standing.hold(networks, previous)
    w = networks[0]["windows"]["24h"]
    assert w["metrics"] == kept["metrics"] and w["series"] == kept["series"]
    assert w["period"] == kept["period"] and w["success_overall"]["score"] == 10.0
    assert {k: w[k] for k in CURRENT} == CURRENT
    assert w["standing"]["status"] == standing.FROZEN and "_held" not in w and "_lead" not in w
    assert w["scores"]["balanced"]["previous_rank"] == 3


def test_a_stopped_window_keeps_its_current_figures_without_scores():
    networks = [held_network(None, standing.STOPPED)]
    standing.hold(networks, None)
    w = networks[0]["windows"]["24h"]
    assert w["scores"] == {} and w["pool"] == CURRENT["pool"] and "_held" not in w


def ranked(scores, frozen=(), average=True):
    """place() over live scores and frozen (score, rank at the stop): each one's rank, in order."""
    live = [{"score": s} for s in scores]
    kept = [({"score": s, "rank": r}, r) for s, r in frozen]
    standing.place(live, kept, average)
    return [s["rank"] for s in live], [s["rank"] for s, _ in kept], live, kept


def test_a_frozen_score_holds_its_place_falls_when_passed_and_never_rises():
    # Held: second of four, below the 9 it was below.
    assert ranked([9, 7, 5], [(8, 2)])[:2] == ([1, 3, 4], [2])
    # Passed: two measured networks now score above its 8, so it falls to third.
    assert ranked([9.5, 9, 7], [(8, 2)])[:2] == ([1, 2, 4], [3])
    # Never rises: the network above it fell below 8; it stays second, no duplicate #1.
    assert ranked([7, 5], [(8, 2)])[:2] == ([1, 3], [2])
    # With fewer networks it keeps its rank number.
    assert ranked([], [(8, 3)])[:2] == ([], [3])


def test_ties_share_by_each_rankings_rule():
    lives, kept, live, _ = ranked([8, 5], [(8, 1.5)])
    assert (lives, kept) == ([1.5, 3], [1.5]) and live[0]["ranked_of"] == 3
    lives, kept, live, _ = ranked([80, 80], [(90, 1)], average=False)
    assert (lives, kept) == ([2, 2], [1]) and "ranked_of" not in live[0]


def test_rerank_moves_only_the_kinds_with_a_frozen_network():
    def net(network, kind, score, rank, status=standing.MEASURED):
        w = {"standing": {"status": status}}
        w["scores"] = {"balanced": {"score": score, "rank": rank, "state": "ready"}}
        w["success_overall"] = {"score": score / 10, "rank": rank, "ranked_of": 2}
        return {"id": network, "kind": kind, "windows": {"24h": w}}

    networks = [net("a", "isp", 90, 1), net("b", "isp", 80, 2), net("c", "isp", 85, 1, "frozen")]
    networks += [net("d", "mobile", 70, 2), net("e", "mobile", 75, 1)]
    standing.rerank(networks)
    ranks = {n["id"]: n["windows"]["24h"]["scores"]["balanced"]["rank"] for n in networks}
    assert ranks == {"a": 1, "b": 3, "c": 2, "d": 2, "e": 1}
    overall = {n["id"]: n["windows"]["24h"]["success_overall"] for n in networks}
    assert [overall[k]["rank"] for k in "abc"] == [1, 3, 2]
    assert overall["b"]["ranked_of"] == 3 and overall["d"]["ranked_of"] == 2


def test_point_scores_rank_measured_windows_only():
    def net(network, rate, status=standing.MEASURED):
        point = {"at": AS_OF, "success_rate": rate, "completed": 200}
        w = {"standing": {"status": status}, "series": [point]}
        return {"id": network, "kind": "isp", "windows": {"24h": w}}

    networks = [net("a", 0.9), net("b", 0.6), net("c", 0.3), net("d", 0.99, standing.FROZEN)]
    standing.point_scores(networks)
    scores = [n["windows"]["24h"]["series"][0].get("success_score") for n in networks]
    assert [s["score"] for s in scores[:3]] == [10, 5.5, 1]
    assert {s["status"] for s in scores[:3]} == {"ranked"} and scores[3] is None


def test_point_scores_share_first_place_as_the_api_does():
    """Two networks tie for first: the API shifts the group's scores so the best reads 10."""

    def net(network, rate):
        point = {"at": AS_OF, "success_rate": rate, "completed": 2000}
        return {"id": network, "kind": "isp", "windows": {"24h": {"series": [point]}}}

    networks = [net("a", 0.9), net("b", 0.9), net("c", 0.5), net("d", 0.2)]
    standing.point_scores(networks)
    scores = [n["windows"]["24h"]["series"][0]["success_score"]["score"] for n in networks]
    assert scores == pytest.approx([10, 10, 5.5, 2.5])


def test_coverage_counts_from_the_first_request_in_the_window():
    """A network measured for two days and then three hours late is covered in its 7d window (45
    of its 48 hours, not 45 of 168): it lifts once measured again instead of staying frozen."""
    rows = [{"bucket_start": AS_OF - (k + 1) * HOUR} for k in range(48) if k not in (3, 4, 5)]
    assert standing.hours(rows, AS_OF) == (45, 48) and standing.hours([], AS_OF) == (0, 0)
    young = entry(AS_OF - HOUR)
    young["windows"]["7d"] = window(AS_OF - HOUR, 45, 168) | {"_hours": standing.hours(rows, AS_OF)}
    held = stored(standing.FROZEN)
    held["windows"]["7d"] = held["windows"]["24h"]
    standing.mark([young], {held["id"]: {"held": held, "measured": None}}, AS_OF, CONFIG)
    assert young["windows"]["7d"]["standing"]["status"] == standing.MEASURED
