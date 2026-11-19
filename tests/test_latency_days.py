"""LATENCY-DELTA-24H-1: the 24h latency change compares the last whole UTC day with the day before
it, made once a day at FREEZE_HOUR, and is None with a reason until two whole days exist. It
follows the headline, the pulses' latency of normal requests (HEADLINE-PULSE-1).
LATENCY-DELTA-PERIOD-1: the 7d (30d) change compares the last 7 (30) whole days with the 7 (30)
before the same way. LATENCY-DELTA-FALLBACK-1: until those pass, it compares the last 7 whole days
(30d) or the last day, so it is null only when the day rule is."""

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


def day(start, ms, countries=COUNTRIES, test="pulse"):
    """One pulse per country (test None: a daily test, under load), each at its own hour."""
    return [row(start + n * HOUR, ms, test=test, country=c) for n, c in enumerate(countries)]


def nov(d):
    return datetime(2026, 11, d, tzinfo=UTC)


def test_two_whole_days_give_the_change_of_the_last_day_against_the_one_before():
    rows = day(nov(13), 900) + day(nov(14), 1000) + day(nov(15), 1100)
    delta = latency_day_delta(rows, AS_OF, 30)
    assert delta["pct"] == pytest.approx(10.0)
    assert delta["reason"] is None and delta["basis"] == "normal"
    assert delta["computed_at"] == AS_OF
    assert [(d["start"], d["end"], d["p50"], d["countries"]) for d in delta["days"]] == [
        (nov(14), nov(15), 1000, 5),
        (nov(15), nov(16), 1100, 5),
    ]
    # Faster is negative; exactly two whole days (no test before them) are enough.
    faster = latency_day_delta(day(nov(14), 1000) + day(nov(15), 800), AS_OF, 30)
    assert faster["pct"] == pytest.approx(-20.0) and faster["reason"] is None


def test_one_day_only_is_none_with_needs_two_days():
    for rows in ([], day(nov(15), 1000)):
        delta = latency_day_delta(rows, AS_OF, 30)
        assert delta["pct"] is None and delta["reason"] == NEEDS_TWO_DAYS


def test_a_half_tested_day_never_makes_a_change():
    # A new network whose first tests ran from midday: its first day has 2 of 5 countries.
    new = day(nov(14), 1000, COUNTRIES[3:]) + day(nov(15), 1100)
    delta = latency_day_delta(new, AS_OF, 30)
    assert delta["pct"] is None and delta["reason"] == NEEDS_TWO_DAYS
    assert [d["countries"] for d in delta["days"]] == [2, 5]
    # An older network whose last day missed most of its countries.
    old = day(nov(13), 900) + day(nov(14), 1000) + day(nov(15), 1100, COUNTRIES[:2])
    assert latency_day_delta(old, AS_OF, 30)["reason"] == DAY_NOT_COVERED
    # A country with too few successes is not tested; one missing of five fails (90% of 5 is 4.5).
    few = day(nov(13), 900) + day(nov(14), 1000) + day(nov(15), 1100, COUNTRIES[:4])
    few.append(row(nov(15) + 20 * HOUR, 9000, n=10, country="JP"))
    delta = latency_day_delta(few, AS_OF, 30)
    assert delta["reason"] == DAY_NOT_COVERED and delta["days"][1]["countries"] == 4
    big = [*COUNTRIES, *(f"C{n}" for n in range(15))]  # 20 countries: 18 tested pass
    nearly = day(nov(14), 1000, big) + day(nov(15), 1100, big[:18])
    assert latency_day_delta(nearly, AS_OF, 30)["pct"] == pytest.approx(10.0)


def test_the_change_is_made_once_a_day_at_the_fixed_hour():
    assert day_pair(datetime(2026, 11, 16, 2, 59, tzinfo=UTC)) == (nov(13), nov(14))
    assert day_pair(AS_OF) == (nov(14), nov(15))
    assert day_pair(datetime(2026, 11, 17, 2, 55, tzinfo=UTC)) == (nov(14), nov(15))
    rows = day(nov(14), 1000) + day(nov(15), 1100)
    frozen = latency_day_delta(rows, AS_OF, 30)
    # Later builds of the same day keep it, whatever today's tests measure so far.
    later = rows + day(nov(16), 5000, COUNTRIES[:2])
    for at in (AS_OF + 6 * HOUR, AS_OF + 23 * HOUR + 55 * timedelta(minutes=1)):
        assert latency_day_delta(later, at, 30) == frozen
    moved = latency_day_delta(later, AS_OF + 24 * HOUR, 30)
    assert moved["days"][1]["start"] == nov(16) and moved["reason"] == DAY_NOT_COVERED


def test_the_change_follows_the_headline_latency_of_normal_requests():
    # The pulses make it (+50%); the daily tests' latency under load (-50%) stays out.
    rows = day(nov(14), 1000) + day(nov(15), 1500)
    rows += day(nov(14), 9000, test=None) + day(nov(15), 4500, test=None)
    delta = latency_day_delta(rows, AS_OF, 30)
    assert delta["pct"] == pytest.approx(50.0) and delta["basis"] == "normal"
    # Daily tests alone make no change: the network has no pulse there.
    daily = day(nov(14), 9000, test=None) + day(nov(15), 4500, test=None)
    assert latency_day_delta(daily, AS_OF, 30)["pct"] is None


def test_a_window_reads_its_latency_targets_pulses():
    rows = day(nov(14), 1000) + day(nov(15), 1100)
    rows += day(nov(14), 9000, test=None) + day(nov(15), 9000, test=None)
    rows.append(row(nov(15), 50, target="generic_https"))  # another target stays out
    delta = window_delta(rows, "generic_http", AS_OF, 30)
    assert delta["basis"] == "normal" and delta["pct"] == pytest.approx(10.0)


def week(start, ms, countries=COUNTRIES):
    """Seven days of pulses from start, every country every day."""
    return [r for d in range(7) for r in day(start + d * timedelta(days=1), ms, countries)]


def test_two_whole_weeks_give_the_7d_change_of_the_last_week_against_the_one_before():
    rows = day(nov(1), 700) + week(nov(2), 1000) + week(nov(9), 1100)
    delta = latency_day_delta(rows, AS_OF, 30, 7)
    assert delta["pct"] == pytest.approx(10.0) and delta["reason"] is None
    assert delta["computed_at"] == AS_OF
    assert [(d["start"], d["end"], d["p50"], d["countries"]) for d in delta["days"]] == [
        (nov(2), nov(9), 1000, 5),
        (nov(9), nov(16), 1100, 5),
    ]
    # A country tested on one day of the week counts for the whole week.
    spread = [r for d in range(5) for r in day(nov(2) + d * timedelta(days=1), 1000, COUNTRIES[d])]
    spread += [r for d in range(5) for r in day(nov(9) + d * timedelta(days=1), 800, COUNTRIES[d])]
    assert latency_day_delta(spread, AS_OF, 30, 7)["pct"] == pytest.approx(-20.0)


def span(delta):
    """The length of the periods a change compares, in whole days."""
    return [(d["end"] - d["start"]).days for d in delta["days"]]


def test_7d_with_under_14_days_compares_the_last_day():
    rows = [r for d in range(6) for r in day(nov(9) + d * timedelta(days=1), 1000)]
    delta = latency_day_delta(rows + day(nov(15), 1100), AS_OF, 30, 7)
    assert delta["pct"] == pytest.approx(10.0) and delta["reason"] is None
    assert [d["start"] for d in delta["days"]] == [nov(14), nov(15)] and span(delta) == [1, 1]
    # Null only when the day rule is: no data, or one day only.
    for rows in ([], day(nov(15), 1000)):
        delta = latency_day_delta(rows, AS_OF, 30, 7)
        assert delta["pct"] is None and delta["reason"] == NEEDS_TWO_DAYS and span(delta) == [1, 1]


def test_a_partly_tested_week_compares_the_last_day():
    # A network live since 7 November: its earlier week misses most countries' tests.
    new = day(nov(7), 1000, COUNTRIES[:2]) + week(nov(9), 1100)
    delta = latency_day_delta(new, AS_OF, 30, 7)
    assert delta["pct"] == pytest.approx(0.0) and span(delta) == [1, 1]
    # Live since 5 November: every country tested, but 4 days are not a whole earlier week.
    late = [r for d in range(10) for r in day(nov(5) + d * timedelta(days=1), 1000)]
    late += day(nov(15), 1100)
    assert latency_day_delta(late, AS_OF, 30, 7)["pct"] == pytest.approx(10.0)
    # An older network whose last week lost countries: its last two days pass.
    old = day(nov(1), 900) + week(nov(2), 1000) + week(nov(9), 1100, COUNTRIES[:3])
    delta = latency_day_delta(old, AS_OF, 30, 7)
    assert delta["pct"] == pytest.approx(0.0) and span(delta) == [1, 1]
    # ... and when its last day misses countries too, the day's reason is served.
    old = [r for r in old if r["bucket_start"] < nov(15) or r["country"] == "US"]
    delta = latency_day_delta(old, AS_OF, 30, 7)
    assert delta["pct"] is None and delta["reason"] == DAY_NOT_COVERED and span(delta) == [1, 1]
    # Live from the afternoon of 2 November, the earlier week's first day: weeks compared.
    since_2nd = [r for r in week(nov(2), 1000) if r["bucket_start"] >= nov(2) + 3 * HOUR]
    since_2nd += week(nov(9), 1100)
    delta = latency_day_delta(since_2nd, AS_OF, 30, 7)
    assert delta["pct"] == pytest.approx(10.0) and span(delta) == [7, 7]


def test_the_7d_change_is_made_once_a_day_at_the_fixed_hour():
    assert day_pair(datetime(2026, 11, 16, 2, 59, tzinfo=UTC), 7) == (nov(1), nov(8))
    assert day_pair(AS_OF, 7) == (nov(2), nov(9))
    assert day_pair(datetime(2026, 11, 17, 2, 55, tzinfo=UTC), 7) == (nov(2), nov(9))
    rows = week(nov(2), 1000) + week(nov(9), 1100)
    frozen = latency_day_delta(rows, AS_OF, 30, 7)
    later = rows + day(nov(16), 5000)
    assert latency_day_delta(later, AS_OF + 23 * HOUR, 30, 7) == frozen
    moved = latency_day_delta(later, AS_OF + 24 * HOUR, 30, 7)
    assert [d["start"] for d in moved["days"]] == [nov(3), nov(10)]


def test_30d_compares_weeks_while_the_rows_cover_30_days():
    # The calculator reads 30 days of raw rows (RAW_DAYS), so the earlier 30 days have none.
    rows = [r for d in range(23) for r in day(nov(16) - (30 - d) * timedelta(days=1), 1000)]
    rows += week(nov(9), 1100)
    delta = latency_day_delta(rows, AS_OF, 30, 30)
    assert delta["pct"] == pytest.approx(10.0) and delta["reason"] is None
    assert [(d["start"], d["p50"], d["countries"]) for d in delta["days"]] == [
        (nov(2), 1000, 5),
        (nov(9), 1100, 5),
    ]
    # Given rows of the 30 days before (a day store, #1252), it compares them.
    older = [r for d in range(30) for r in day(nov(16) - (60 - d) * timedelta(days=1), 800)]
    delta = latency_day_delta(older + rows, AS_OF, 30, 30)
    assert span(delta) == [30, 30] and delta["days"][0]["p50"] == 800
    # Under 14 days: the last day; no data: the day rule's reason.
    assert span(latency_day_delta(rows[-10 * 5 :], AS_OF, 30, 30)) == [1, 1]
    assert latency_day_delta([], AS_OF, 30, 30)["reason"] == NEEDS_TWO_DAYS


# LATENCY-ALWAYS-1: a network on 17 Nov 2026 (159 countries tested on 15 Nov, its 15:00 hour
# excluded; 205 on 16 Nov): the countries tested on both days make the day change.
SHIFTER = [f"C{n:03}" for n in range(205)]
MINUTE = timedelta(minutes=1)


def spread(start, ms, countries):
    """One pulse per country, all within the day from start (7 minutes apart)."""
    return [row(start + 7 * n * MINUTE, ms, country=c) for n, c in enumerate(countries)]


def test_a_partly_tested_day_is_compared_on_the_countries_both_days_have():
    rows = spread(nov(13), 900, SHIFTER) + spread(nov(14), 1000, SHIFTER[:159])
    rows += spread(nov(15), 1100, SHIFTER[:159]) + spread(nov(15), 5000, SHIFTER[159:])
    for days in (1, 7, 30):
        delta = latency_day_delta(rows, AS_OF, 30, days)
        assert delta["pct"] == pytest.approx(10.0) and delta["scope"] == "common"
        assert span(delta) == [1, 1] and [d["countries"] for d in delta["days"]] == [159, 159]
    # On all countries the 46 others (5000 ms) would move it: they are not compared.
    assert latency_day_delta(rows, AS_OF, 30)["days"][1]["p50"] == 1100
    # Every build of the day keeps it; the pair moves at 03:00 UTC.
    later = rows + spread(nov(16), 3000, SHIFTER[:3])
    frozen = latency_day_delta(rows, AS_OF, 30, 7)
    assert latency_day_delta(later, AS_OF + 23 * HOUR, 30, 7) == frozen
    assert latency_day_delta(later, AS_OF + 24 * HOUR, 30, 7)["days"][1]["start"] == nov(16)
    # A whole pair still compares every country.
    whole = spread(nov(14), 1000, SHIFTER) + spread(nov(15), 1100, SHIFTER)
    assert latency_day_delta(whole, AS_OF, 30)["scope"] == "all"


def test_too_few_common_countries_give_no_change():
    # Common countries under half of those tested over the two days (8 of 20), or under 5.
    big = [f"C{n:02}" for n in range(20)]
    rows = day(nov(13), 900, big) + day(nov(14), 1000, big[:8]) + day(nov(15), 1100, big)
    delta = latency_day_delta(rows, AS_OF, 30, 7)
    assert delta["pct"] is None and delta["reason"] == DAY_NOT_COVERED and span(delta) == [1, 1]
    rows = day(nov(13), 900) + day(nov(14), 1000, COUNTRIES[:4]) + day(nov(15), 1100)
    assert latency_day_delta(rows, AS_OF, 30)["reason"] == DAY_NOT_COVERED
    # Half of them is enough (10 of 20).
    rows = day(nov(13), 900, big) + day(nov(14), 1000, big[:10]) + day(nov(15), 1100, big)
    assert latency_day_delta(rows, AS_OF, 30)["pct"] == pytest.approx(10.0)


def test_a_partly_tested_week_is_compared_on_its_common_countries_before_the_day():
    big = [f"C{n:02}" for n in range(20)]
    rows = day(nov(1), 900, big) + week(nov(2), 1000, big[:15]) + week(nov(9), 1100, big)
    delta = latency_day_delta(rows, AS_OF, 30, 7)
    assert delta["pct"] == pytest.approx(10.0) and delta["scope"] == "common"
    assert span(delta) == [7, 7]
