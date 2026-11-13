"""DAY-SCORE-1: each network's days scored as its windows are (target_scores.score_days()): a
day's success_overall is the all-targets overall of a one-day window, the networks of one kind
ranked per day; a day without browser runs counts the HTTP targets it has and says so."""

import copy
from pathlib import Path

from test_calculated_success_scores import network

from atlas.calculated import target_scores as ts
from atlas.calculated.policy import load

CONFIG, _ = load(Path("configs/methodology.toml"))
DAYS = ("2026-11-08T00:00:00+00:00", "2026-11-09T00:00:00+00:00", "2026-11-10T00:00:00+00:00")


def entry(kind, *windows):
    """A network entry whose days have the windows' targets (None: a day without targets)."""
    days = zip(DAYS, windows, strict=True)
    days = [{"start": s, "targets": w["targets"] if w else {}} for s, w in days]
    return {"kind": kind, "days": days}


def test_a_day_is_scored_as_a_one_day_window():
    rates = [(0.9, 0.3, 0.9), (0.6, 0.6, 0.6), (0.3, 0.9, 0.3), (0.5, 0.5, 0.2)]
    windows = [network(*r) for r in rates]
    http_only = [network(h, s) for h, s, _ in rates]  # no browser run that day
    nets = [entry("residential", w, h, None) for w, h in zip(windows, http_only, strict=True)]
    nets.append(entry("mobile", network(0.1, 0.1, 0.1), network(0.1, 0.1), None))
    expected = [{"kind": "residential", "windows": {"24h": copy.deepcopy(w)}} for w in windows]
    ts.score_networks(expected, CONFIG)
    ts.score_days(nets, CONFIG)
    for net, want in zip(nets, expected, strict=False):
        assert [list(d) for d in net["days"]] == [["start", "success_overall"]] * len(DAYS)
        assert [d["start"] for d in net["days"]] == list(DAYS)
        overall = want["windows"]["24h"]["success_overall"]
        assert net["days"][0]["success_overall"] == {
            k: v for k, v in overall.items() if k != "weights"
        }
        assert overall["status"] == ts.RANKED and set(overall["targets"]) > set(ts.HTTP_TARGETS)
        # The HTTP-only day counts the HTTP targets alone, ranked among the same networks.
        day = net["days"][1]["success_overall"]
        assert set(day["targets"]) == set(ts.HTTP_TARGETS) and day["ranked_of"] == 4
        # The third day has no target at all: no overall.
        assert net["days"][2]["success_overall"] is None
    assert nets[0]["days"][1]["success_overall"]["rank"] == 2.5  # 0.9 and 0.3 against 0.3, 0.9
    # The mobile network is ranked against no residential one: too few networks.
    mobile = nets[-1]["days"][0]["success_overall"]
    assert mobile["status"] == ts.NO_RANKED_TARGETS and mobile["targets"] == {}


def test_a_network_without_days_keeps_none():
    nets = [{"kind": "residential", "days": []}]
    ts.score_days(nets, CONFIG)
    assert nets == [{"kind": "residential", "days": []}]
