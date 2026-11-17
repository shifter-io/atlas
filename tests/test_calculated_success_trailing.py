"""SUCCESS-TRAILING-1 (#1301, the owner, 2026-11-17): each network series point is the window's
Success x/10 rule, and the window's success rate, over the TRAILING window of the window's length
ending at the point (network.point_trailing(), target_scores.point_overall()), the best network
the kind's best over that same window: the newest point is the headline and a steady network's
line reads its headline, whatever country mix each hour's daily tests had."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from atlas.calculated import network
from atlas.calculated import target_scores as ts
from atlas.calculated.aggregate import in_window, series_axis, timed
from atlas.calculated.policy import load

CONFIG = load(Path("configs/methodology.toml"))[0]
END = datetime(2026, 11, 17, 10, 10, tzinfo=UTC)
HOUR = timedelta(hours=1)


def row(at, percent, completed=1000):
    counters = {"completed": completed, "successes": round(completed * percent / 100)}
    counters |= {"response_body_bytes": 0, "errors": {}, "latency_histogram": {"bins": []}}
    found = {"bucket_start": at, "slot": at, "test": None, "target": "generic_http"}
    return found | {"counters": counters, "last_observed": at}


def net(base, swing=0.5):
    """A network whose daily tests of each hour, over the 48 hours before END, read base +- swing
    points by the hour's country mix (even hours easier, odd ones harder): steady over any 24."""
    start = END - 48 * HOUR
    rows = [row(start + h * HOUR, base + (swing if h % 2 == 0 else -swing)) for h in range(48)]
    return SimpleNamespace(rows=rows, pulses=[])


def entry(n, hours=24):
    since = END - timedelta(hours=hours)
    first, step = series_axis(END, hours)
    points = [{"at": first + k * step} for k in range(24 if hours == 24 else hours // 24)]
    network.point_trailing(points, n, END, hours)
    rows = in_window(n.rows, since, END, hours)
    window = {"targets": {}, "_daily": ts.daily_counts(rows), "series": points}
    window["_rate"] = timed(rows, hours, 1, (), None, since)["success_rate"]
    return {"kind": "residential", "windows": {f"{hours}h": window}}


def test_a_steady_network_is_a_flat_line_at_its_headline():
    found = [entry(net(99.0)), entry(net(98.0)), entry(net(95.0))]
    ts.score_networks(found, CONFIG)
    for e in found:
        w = e["windows"]["24h"]
        headline = w["success_overall"]["score"]
        scores = [p["success_score"]["score"] for p in w["series"]]
        assert all(abs(s - headline) <= 0.3 for s in scores), (headline, scores)
        assert scores[-1] == headline  # the newest point is the headline, exactly
        assert w["series"][-1]["trailing_success_rate"] == pytest.approx(w["_rate"], abs=1e-12)
        assert all("_daily" not in p for p in w["series"])


def test_one_hard_hour_moves_a_trailing_point_a_24th_as_much():
    """A network with one hard hour (5 points down, its daily tests at 09:10): an own-hour score
    would drop 5 points; the trailing points holding it drop 5 / 24 (one hour of 24)."""
    hard = net(98.0, swing=0.0)
    hard.rows[-1] = row(hard.rows[-1]["bucket_start"], 93.0)
    found = [entry(net(99.0, swing=0.0)), entry(hard), entry(net(95.0, swing=0.0))]
    ts.score_networks(found, CONFIG)
    scores = [p["success_score"]["score"] for p in found[1]["windows"]["24h"]["series"]]
    assert scores[21] == pytest.approx(9.0, abs=1e-6)  # its window ends at 09:00, before it
    assert 9.0 - scores[-1] == pytest.approx(5 / 24, abs=1e-6)
    assert scores[-2] == pytest.approx(scores[-1], abs=1e-9)


def test_a_trailing_window_reaching_before_the_rows_kept_has_the_rows_that_exist():
    """7d: the daily points' 7-day trailing windows reach 13 days back, past these 48 hours of
    rows: each reads the rows it has, the newest still the window's."""
    found = [entry(net(99.0), 168), entry(net(98.0), 168), entry(net(95.0), 168)]
    ts.score_networks(found, CONFIG)
    w = found[1]["windows"]["168h"]
    assert w["series"][-1]["success_score"]["score"] == w["success_overall"]["score"]
    assert w["series"][0]["success_score"] == {"score": None, "status": ts.NO_RANKED_TARGETS}
