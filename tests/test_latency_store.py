"""LATENCY-DAY-STORE-1: the 30d latency change compares the last 30 whole UTC days with the 30
before, from per-day records: the raw rows' whole days and, before them, the stored days
(calculated_latency_days), which the first run fills from the 30 days of raw rows it reads."""

from datetime import UTC, datetime, timedelta

import pytest
from scoring_cases import row
from test_latency_days import COUNTRIES, HOUR, day

from atlas.calculated.latency import blended
from atlas.calculated.latency_days import DAY_NOT_COVERED, NEEDS_TWO_PERIODS
from atlas.calculated.latency_store import KEEP_DAYS, _period, complete, day_records, delta
from atlas.calculated.underload import light_votes, time_slots
from atlas.storage import connect

pytest_plugins = ["test_storage", "calculated_template"]

DAY = timedelta(days=1)
AS_OF = datetime(2027, 1, 14, 3, tzinfo=UTC)  # compares [15 Nov, 15 Dec) with [15 Dec, 14 Jan)
TARGET = "generic_http"
STORED = "SELECT day,as_of FROM calculated_latency_days ORDER BY day"


@pytest.fixture
def database(calculated_database):
    return calculated_database


def dec(d):
    return datetime(2026, 12, d, tzinfo=UTC)


def days(first, count, ms, countries=COUNTRIES):
    return [r for n in range(count) for r in day(first + n * DAY, ms, countries)]


def history():
    """14 Nov to 13 Jan: 1000 ms until 14 Dec, 1100 ms from 15 Dec."""
    return days(dec(1) - 17 * DAY, 31, 1000) + days(dec(15), 30, 1100)


def records(rows, as_of=AS_OF, minimum=30):
    return day_records(rows, TARGET, rows[0]["bucket_start"], as_of, minimum)["days"]


def test_a_period_of_day_records_is_the_blend_of_all_its_slots():
    # 40 slots of 7 days, each country at its own latency; each slot weighs the same.
    mixed = [
        row(dec(1) + n * 4 * HOUR, 900 + 37 * (n % 3), country=COUNTRIES[n % 5]) for n in range(40)
    ]
    mixed.append(row(dec(3) + HOUR, 5000, n=10))  # a slot with too few successes does not vote
    found = _period(records(mixed, dec(20)), dec(1), 7, 30)
    expected = blended(h for *_, h in light_votes(time_slots(mixed), 30))["p50"]
    assert found["p50"] == expected and found["countries"] == sorted(COUNTRIES)
    assert (found["start"], found["end"]) == (dec(1), dec(8))


def test_two_whole_30_day_periods_give_the_change():
    found = delta(records(history()), AS_OF, 30)
    assert found["pct"] == pytest.approx(10.0) and found["reason"] is None
    assert found["computed_at"] == AS_OF
    assert [(d["start"], d["end"], d["p50"], d["countries"]) for d in found["days"]] == [
        (dec(15) - 30 * DAY, dec(15), 1000, 5),
        (dec(15), dec(15) + 30 * DAY, 1100, 5),
    ]
    # The pair moves once a day, at the fixed hour.
    later = delta(records(history()), AS_OF + 23 * HOUR, 30)
    assert later == found
    assert delta(records(history()), AS_OF - HOUR, 30)["days"][0]["start"] == dec(14) - 30 * DAY


def test_one_period_only_is_none_with_needs_two_periods():
    for rows in (days(dec(15), 30, 1100), days(dec(1), 44, 1100)):
        # 1 Dec: the earlier period's last 14 days pass the country rule, but it is not whole.
        found = delta(records(rows), AS_OF, 30)
        assert found["pct"] is None and found["reason"] == NEEDS_TWO_PERIODS
    assert delta([], AS_OF, 30)["reason"] == NEEDS_TWO_PERIODS
    # An older network whose last period missed most of its countries.
    few = days(dec(1) - 17 * DAY, 31, 1000) + days(dec(15), 30, 1100, COUNTRIES[:2])
    assert delta(records(few), AS_OF, 30)["reason"] == DAY_NOT_COVERED


def build(conn, rows, as_of, minimum=30):
    """complete() over the rows a build at as_of reads (its 30 days); the 30d change it makes."""
    since = as_of - 30 * DAY
    own = [r for r in rows if since <= r["bucket_start"] < as_of]
    window = {"_latency_days": day_records(own, TARGET, since, as_of, minimum)}
    complete(conn, [{"provider": "p", "kind": "k", "windows": {"30d": window}}], as_of)
    assert "_latency_days" not in window
    return window["latency_day_delta"]


def test_the_store_keeps_the_days_the_raw_rows_no_longer_hold(database):
    rows, first, middle = history(), dec(14), dec(20)
    with connect(database) as conn:
        # The first run fills the store with the whole days of its 30 days of raw rows.
        assert build(conn, rows, first)["reason"] == NEEDS_TWO_PERIODS
        stored = conn.execute(STORED).fetchall()
        assert stored == [(dec(14) - (30 - n) * DAY, first) for n in range(30)]
        # A late test of 25 Nov is written again; unchanged days are not.
        late = [*rows, row(dec(1) - 6 * DAY + 20 * HOUR, 4000, country="US")]
        build(conn, late, middle)
        stored = dict(conn.execute(STORED).fetchall())
        assert stored[dec(1) - 6 * DAY] == middle and stored[dec(1) - 7 * DAY] == first
        assert stored[dec(19)] == middle and len(stored) == 36
        # A replay of an earlier cutoff never replaces a later one's day.
        build(conn, rows, first)
        assert conn.execute(STORED).fetchall() == list(stored.items())
        # 60 days on, only the last 30 are raw rows: the 30 before come from the store.
        found = build(conn, late, AS_OF)
        assert found == delta(records(late), AS_OF, 30) and found["pct"] == pytest.approx(10.0)
        assert len(conn.execute(STORED).fetchall()) == 61  # 14 Nov to 13 Jan


def test_the_store_forgets_days_before_its_62_and_another_minimum(database):
    rows = history()
    with connect(database) as conn:
        for at in (dec(14), dec(20), AS_OF):
            build(conn, rows, at)
        # Before 03:00 UTC the pair is still the day before's, its earlier period still stored.
        assert build(conn, rows, AS_OF + 2 * DAY - 2 * HOUR)["pct"] == pytest.approx(10.0)
        build(conn, rows, AS_OF + 2 * DAY)
        assert conn.execute(STORED).fetchone()[0] == AS_OF + 2 * DAY - 3 * HOUR - KEEP_DAYS * DAY
        # Days stored with another min_latency_successes do not count: the earlier period is
        # not there.
        assert build(conn, rows, AS_OF + 2 * DAY, minimum=20)["reason"] == NEEDS_TWO_PERIODS
