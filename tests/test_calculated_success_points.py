"""SUCCESS-POINTS-1 (#1301, the owner, 2026-11-17): each network series point's success_score is
the window's Success x/10 rule (target_scores.success_overall()) over the point's card target
counts ("_success", SUCCESS-ALL-8-1), the best network the kind's best over the same counts.
Which rows a point counts (its trailing window, SUCCESS-TRAILING-1) is
test_calculated_success_trailing.py's."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from atlas.calculated import target_scores as ts
from atlas.calculated.policy import load, success_score

CONFIG = load(Path("configs/methodology.toml"))[0]
HOURS = [datetime(2026, 11, 17, h, tzinfo=UTC) for h in range(3)]


def counts(percent, completed=10_000):
    return {"generic_http": [completed, round(completed * percent / 100)]}


def entry(window_rate, hourly):
    """A residential network: its 24h window's generic_http at window_rate and one series point
    per hour at each of hourly's rates (None: no try that hour)."""
    daily = [counts(r) if r is not None else {} for r in hourly]
    points = [{"at": t, "_success": d} for t, d in zip(HOURS, daily, strict=True)]
    window = {"targets": {}, "_success": counts(window_rate, 30_000), "series": points}
    return {"kind": "residential", "windows": {"24h": window}}


def scores(found, i):
    return [p["success_score"]["score"] for p in found[i]["windows"]["24h"]["series"]]


def test_a_steady_network_reads_its_headline_at_every_point():
    found = [entry(99.0, [99.0] * 3), entry(98.0, [98.0] * 3), entry(95.0, [95.0] * 3)]
    ts.score_networks(found, CONFIG)
    for i, net in enumerate(found):
        headline = net["windows"]["24h"]["success_overall"]["score"]
        assert scores(found, i) == pytest.approx([headline] * 3, abs=0.05)
        assert all("_success" not in p for p in net["windows"]["24h"]["series"])
        # generic_http alone: the other card targets are missing, the point says so.
        assert {p["success_score"]["status"] for p in net["windows"]["24h"]["series"]} == {
            ts.PARTIAL_TARGETS
        }


def test_a_point_moves_with_the_hours_tries_within_the_band():
    """Every network within +-d points of its window rate each hour: each point stays within
    2 x slope x d of its headline (its rate and the best's each move by at most d)."""
    d, slope = 0.5, success_score(CONFIG)["slope"]
    found = [
        entry(99.0, [99.5, 98.5, 99.0]),
        entry(98.0, [97.5, 98.5, 98.0]),
        entry(95.0, [95.5, 94.5, 95.0]),
    ]
    ts.score_networks(found, CONFIG)
    for i, net in enumerate(found):
        headline = net["windows"]["24h"]["success_overall"]["score"]
        assert all(abs(s - headline) <= 2 * slope * d + 1e-9 for s in scores(found, i))
    assert scores(found, 0) == pytest.approx([10, 10, 10])  # the best of each hour reads 10


def test_an_hour_without_a_try_has_no_score():
    found = [entry(99.0, [99.0, None, 99.0]), entry(98.0, [98.0, None, 98.0])]
    found[1]["windows"]["24h"]["series"][2]["_success"] = counts(98.0, completed=0)
    ts.score_networks(found, CONFIG)
    gap = found[0]["windows"]["24h"]["series"][1]["success_score"]
    few = found[1]["windows"]["24h"]["series"][2]["success_score"]
    assert gap == {"score": None, "status": ts.NO_RANKED_TARGETS}
    assert few == {"score": None, "status": ts.NO_RANKED_TARGETS}
    assert scores(found, 0)[2] == 10  # alone with a value that hour, it is its best
