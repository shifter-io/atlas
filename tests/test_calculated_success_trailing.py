"""SUCCESS-TRAILING-1 (#1301, the owner, 2026-11-17): each network series point is the window's
Success x/10 rule, and the window's success rate, over the TRAILING window of the window's length
ending at the point (network.point_trailing(), target_scores.point_overall()), the best network
the kind's best over that same window: the newest point is the headline and a steady network's
line reads its headline, whatever country mix each hour's requests had. Its counts are the card
targets' (SUCCESS-ALL-8-1): generic_http here, a browser target over the 7 days in the last test."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from atlas.calculated import card_success, network
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
    return SimpleNamespace(rows=rows, pulses=[], browser=[])


def entry(n, hours=24):
    since = END - timedelta(hours=hours)
    first, step = series_axis(END, hours)
    points = [{"at": first + k * step} for k in range(24 if hours == 24 else hours // 24)]
    network.point_trailing(points, n, CONFIG, END, hours)
    rows = in_window(n.rows, since, END, hours)
    http = card_success.http_counts(CONFIG, rows)
    own = card_success.browser_since(CONFIG, END, hours)
    window = {"targets": {}, "series": points}
    window["_success"] = card_success.counts(CONFIG, http, n.browser, own, END)
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
        assert all("_success" not in p for p in w["series"])


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


def test_a_point_without_a_whole_day_of_data_is_empty():
    """SUCCESS-FULL-DAY-1: these networks' daily tests start 48 hours before END (15 Nov 10:10).
    The 7d days before, and the day the data starts in (it holds 14 hours: a few countries), are
    null with not_enough_data, never a score, and do not count for the best; from the next day
    each point has its trailing window's rows, the newest still the window's."""
    found = [entry(net(99.0), 168), entry(net(98.0), 168), entry(net(95.0), 168)]
    ts.score_networks(found, CONFIG)
    w = found[1]["windows"]["168h"]
    empty = {"score": None, "status": ts.NOT_ENOUGH_DATA}
    assert [p["success_score"] for p in w["series"][:5]] == [empty] * 5  # 11 to 15 Nov
    assert all(p["trailing_success_rate"] is None for p in w["series"][:5])
    assert w["series"][5]["success_score"]["status"] == ts.PARTIAL_TARGETS  # 16 Nov: 38 hours
    assert w["series"][-1]["success_score"]["score"] == w["success_overall"]["score"]
    assert all("_short" not in p for p in w["series"])


def test_a_short_first_day_does_not_make_another_networks_point_empty_or_its_best():
    """A network whose data starts later (its first 6 hours of daily tests, a hard country) is
    empty that day; the others' points of that day are ranked among themselves only."""
    late = net(80.0, swing=0.0)
    late.rows = late.rows[-6:]  # only its last 6 hours: less than a day
    found = [entry(net(99.0), 168), entry(net(98.0), 168), entry(late, 168)]
    ts.score_networks(found, CONFIG)
    newest = [e["windows"]["168h"]["series"][-1]["success_score"] for e in found]
    assert newest[2] == {"score": None, "status": ts.NOT_ENOUGH_DATA}
    assert newest[0]["score"] == 10 and newest[1]["score"] == pytest.approx(9.0)


def test_a_24h_point_reads_its_browser_targets_over_the_trailing_7_days():
    """A LinkedIn run 3 days before END is in every 24h point's browser window (7 days back from
    the point's end), so each point counts it as the window does: not partial for it."""
    runs = []
    for k, rate in enumerate((14.0, 7.0)):
        n = net(99.0 - k, swing=0.0)
        ended = END - timedelta(days=3)
        counters = {"completed": 100, "successes": round(rate)}
        n.browser = [{"target": "linkedin_public", "test": "browser", "bucket_start": ended - HOUR}]
        n.browser[0] |= {"run_finished_at": ended, "counters": counters}
        runs.append(entry(n))
    ts.score_networks(runs, CONFIG)
    for e in runs:
        w = e["windows"]["24h"]
        assert "linkedin_public" in w["success_overall"]["targets"]
        assert w["series"][-1]["success_score"]["score"] == w["success_overall"]["score"]
    # 99% / 14% against 98% / 7%: the mean gap (1 + 7) / 2 behind the best.
    behind = runs[1]["windows"]["24h"]["success_overall"]["score"]
    assert behind == pytest.approx(10 - (1 + 7) / 2)
