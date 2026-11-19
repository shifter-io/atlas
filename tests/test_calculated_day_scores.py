"""DAY-SCORE-1: each network's days scored as its windows are (target_scores.score_days()): a
day's success_overall is a one-day window's, from that day's card target tries (SUCCESS-ALL-8-1),
the networks of one kind scored against each other per day; a day without a try has none."""

import copy
from pathlib import Path

import pytest
from test_calculated_success_scores import network

from atlas.calculated import target_scores as ts
from atlas.calculated.policy import load

CONFIG, _ = load(Path("configs/methodology.toml"))
DAYS = ("2026-11-08T00:00:00+00:00", "2026-11-09T00:00:00+00:00", "2026-11-10T00:00:00+00:00")


def daily(rate, completed=1000):
    """A window's or day's "_success": generic_http's [completed, successes]."""
    return {"generic_http": [completed, round(completed * rate)]} if rate is not None else {}


def entry(kind, *rates):
    """A network entry whose days have generic_http tries at the rates (None: a day without)."""
    days = zip(DAYS, rates, strict=True)
    targets = network(0.9, 0.9)["targets"]
    days = [{"start": s, "targets": copy.deepcopy(targets), "_success": daily(r)} for s, r in days]
    return {"kind": kind, "days": days}


def test_a_day_is_scored_as_a_one_day_window():
    rates = [(0.99, 0.95), (0.98, 0.99), (0.95, 0.95), (0.90, 0.99)]
    nets = [entry("residential", a, b, None) for a, b in rates]
    nets.append(entry("mobile", 0.5, 0.5, None))
    expected = [
        {"kind": "residential", "windows": {"24h": network(0.9, 0.9) | {"_success": daily(a)}}}
        for a, _ in rates
    ]
    ts.score_networks(expected, CONFIG)
    ts.score_days(nets, CONFIG)
    for net, want in zip(nets, expected, strict=False):
        assert [list(d) for d in net["days"]] == [["start", "success_overall"]] * len(DAYS)
        assert [d["start"] for d in net["days"]] == list(DAYS)
        overall = want["windows"]["24h"]["success_overall"]
        assert net["days"][0]["success_overall"] == {
            k: v for k, v in overall.items() if k != "weights"
        }
        assert overall["status"] == ts.PARTIAL_TARGETS and set(overall["targets"]) == {"generic_http"}
        # The third day has no try: a null score with its reason.
        none = net["days"][2]["success_overall"]
        assert none["score"] is None and none["status"] == ts.NO_RANKED_TARGETS
    # Day 1: 0.95 and 0.99 against the best 0.99, each day its own leader.
    day = [n["days"][1]["success_overall"]["score"] for n in nets[:4]]
    assert day == pytest.approx([6, 10, 6, 10])
    assert [n["days"][1]["success_overall"]["rank"] for n in nets[:4]] == [3.5, 1.5, 3.5, 1.5]
    # The mobile network is scored against no residential one: its own leader.
    mobile = nets[-1]["days"][0]["success_overall"]
    assert mobile["score"] == 10 and mobile["ranked_of"] == 1


def test_a_network_without_days_keeps_none():
    nets = [{"kind": "residential", "days": []}]
    ts.score_days(nets, CONFIG)
    assert nets == [{"kind": "residential", "days": []}]
