"""LATENCY-DELTA-24H-1: the 24h latency change compares the last whole UTC day with the day before
it, made once a day at FREEZE_HOUR, and is None with a reason until two whole days exist."""

from datetime import UTC, datetime, timedelta

import pytest
from scoring_cases import row

from atlas.calculated.latency_days import (
    DAY_NOT_COVERED,
    NEEDS_TWO_DAYS,
    day_pair,
    latency_day_delta,
    window_delta,
)

AS_OF = datetime(2026, 11, 16, 3, tzinfo=UTC)  # compares 14 and 15 November
COUNTRIES = ("US", "DE", "FR", "GB", "JP")
HOUR = timedelta(hours=1)


def day(start, ms, countries=COUNTRIES, test=None):
    """One daily test per country (test None: under load), each at its own hour of the day."""
    return [row(start + n * HOUR, ms, test=test, country=c) for n, c in enumerate(countries)]


def nov(d):
    return datetime(2026, 11, d, tzinfo=UTC)


def test_two_whole_days_give_the_change_of_the_last_day_against_the_one_before():
    rows = day(nov(13), 900) + day(nov(14), 1000) + day(nov(15), 1100)
    delta = latency_day_delta(rows, AS_OF, 30, under=True)
    assert delta["pct"] == pytest.approx(10.0)
    assert delta["reason"] is None and delta["basis"] == "under_load"
    assert delta["computed_at"] == AS_OF
    assert [(d["start"], d["end"], d["p50"], d["countries"]) for d in delta["days"]] == [
        (nov(14), nov(15), 1000, 5),
        (nov(15), nov(16), 1100, 5),
    ]
    # Faster is negative; exactly two whole days (no test before them) are enough.
    faster = latency_day_delta(day(nov(14), 1000) + day(nov(15), 800), AS_OF, 30, under=True)
    assert faster["pct"] == pytest.approx(-20.0) and faster["reason"] is None


def test_one_day_only_is_none_with_needs_two_days():
    for rows in ([], day(nov(15), 1000)):
        delta = latency_day_delta(rows, AS_OF, 30, under=True)
        assert delta["pct"] is None and delta["reason"] == NEEDS_TWO_DAYS


def test_a_half_tested_day_never_makes_a_change():
    # A new network whose first tests ran from midday: its first day has 2 of 5 countries.
    new = day(nov(14), 1000, COUNTRIES[3:]) + day(nov(15), 1100)
    delta = latency_day_delta(new, AS_OF, 30, under=True)
    assert delta["pct"] is None and delta["reason"] == NEEDS_TWO_DAYS
    assert [d["countries"] for d in delta["days"]] == [2, 5]
    # An older network whose last day missed most of its countries.
    old = day(nov(13), 900) + day(nov(14), 1000) + day(nov(15), 1100, COUNTRIES[:2])
    assert latency_day_delta(old, AS_OF, 30, under=True)["reason"] == DAY_NOT_COVERED
    # A country with too few successes is not tested; one missing of five fails (90% of 5 is 4.5).
    few = day(nov(13), 900) + day(nov(14), 1000) + day(nov(15), 1100, COUNTRIES[:4])
    few.append(row(nov(15) + 20 * HOUR, 9000, n=10, test=None, country="JP"))
    delta = latency_day_delta(few, AS_OF, 30, under=True)
    assert delta["reason"] == DAY_NOT_COVERED and delta["days"][1]["countries"] == 4
    big = [*COUNTRIES, *(f"C{n}" for n in range(15))]  # 20 countries: 18 tested pass
    nearly = day(nov(14), 1000, big) + day(nov(15), 1100, big[:18])
    assert latency_day_delta(nearly, AS_OF, 30, under=True)["pct"] == pytest.approx(10.0)


def test_the_change_is_made_once_a_day_at_the_fixed_hour():
    assert day_pair(datetime(2026, 11, 16, 2, 59, tzinfo=UTC)) == (nov(13), nov(14))
    assert day_pair(AS_OF) == (nov(14), nov(15))
    assert day_pair(datetime(2026, 11, 17, 2, 55, tzinfo=UTC)) == (nov(14), nov(15))
    rows = day(nov(14), 1000) + day(nov(15), 1100)
    frozen = latency_day_delta(rows, AS_OF, 30, under=True)
    # Later builds of the same day keep it, whatever today's tests measure so far.
    later = rows + day(nov(16), 5000, COUNTRIES[:2])
    for at in (AS_OF + 6 * HOUR, AS_OF + 23 * HOUR + 55 * timedelta(minutes=1)):
        assert latency_day_delta(later, at, 30, under=True) == frozen
    moved = latency_day_delta(later, AS_OF + 24 * HOUR, 30, under=True)
    assert moved["days"][1]["start"] == nov(16) and moved["reason"] == DAY_NOT_COVERED


def test_the_change_follows_the_headline_latency():
    # Normal requests (pulses) when the headline is not under load; the daily tests stay out.
    rows = day(nov(14), 1000, test="pulse") + day(nov(15), 1500, test="pulse")
    rows += day(nov(14), 9000) + day(nov(15), 9000)
    normal = latency_day_delta(rows, AS_OF, 30, under=False)
    assert normal["pct"] == pytest.approx(50.0) and normal["basis"] == "normal"
    assert latency_day_delta(rows, AS_OF, 30, under=True)["pct"] == pytest.approx(0.0)


def test_a_window_follows_its_latency_targets_headline():
    rows = day(nov(14), 1000) + day(nov(15), 1100)
    rows += day(nov(14), 9000, test="pulse") + day(nov(15), 9000, test="pulse")
    rows.append(row(nov(15), 50, test=None, target="generic_https"))  # another target stays out
    loaded = {"generic_http": {"metrics": {"latency_under_load_ms": {"p50": 1050}}}}
    under = window_delta(rows, loaded, "generic_http", AS_OF, 30)
    assert under["basis"] == "under_load" and under["pct"] == pytest.approx(10.0)
    light = {"generic_http": {"metrics": {"latency_under_load_ms": {"p50": None}}}}
    for targets in (light, {}):
        normal = window_delta(rows, targets, "generic_http", AS_OF, 30)
        assert normal["basis"] == "normal" and normal["pct"] == pytest.approx(0.0)
