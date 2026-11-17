"""LATENCY-DAY-STORE-1: the 30d latency change compares the last 30 whole UTC days with the 30
before, from per-day records: the raw rows' whole days and, before them, the stored days
(calculated_latency_days), which the first run fills from the 30 days of raw rows it reads.
LATENCY-DELTA-FALLBACK-1: until two 30-day periods pass, it compares the last 7 whole days, then
the last day, each network by its own history."""

from datetime import UTC, datetime, timedelta

import pytest
from scoring_cases import row
from test_latency_days import COUNTRIES, HOUR, day, span

from atlas.calculated.latency import blended
from atlas.calculated.latency_days import DAY_NOT_COVERED, NEEDS_TWO_DAYS
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
    found = _period(records(mixed, dec(20)), 30, dec(1), 7)
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


def test_under_60_days_compares_weeks_and_under_14_days_the_last_day():
    # 30 days (15 Dec to 13 Jan): the last 7 whole days against the 7 before.
    weeks = delta(records(days(dec(15), 23, 1000) + days(dec(15) + 23 * DAY, 7, 1100)), AS_OF, 30)
    assert weeks["pct"] == pytest.approx(10.0) and span(weeks) == [7, 7]
    # 1 Dec: the earlier period's last 14 days pass the country rule, but it is not whole.
    assert span(delta(records(days(dec(1), 44, 1100)), AS_OF, 30)) == [7, 7]
    # 13 days (1 to 13 Jan): the last day against the day before.
    short = days(dec(15) + 17 * DAY, 12, 1000) + days(dec(15) + 29 * DAY, 1, 1100)
    found = delta(records(short), AS_OF, 30)
    assert found["pct"] == pytest.approx(10.0) and span(found) == [1, 1]
    # Null only when the day rule is.
    for own in ([], records(days(dec(15) + 29 * DAY, 1, 1100))):
        found = delta(own, AS_OF, 30)
        assert found["pct"] is None and found["reason"] == NEEDS_TWO_DAYS
    # An older network that lost countries over its last 30 days, more over its last week, and
    # was not tested on 13 Jan: no period passes.
    few = days(dec(1) - 17 * DAY, 31, 1000) + days(dec(15), 23, 1100, COUNTRIES[:2])
    few += days(dec(15) + 23 * DAY, 6, 1100, COUNTRIES[:1])
    found = delta(records(few), AS_OF, 30)
    assert found["reason"] == DAY_NOT_COVERED and span(found) == [1, 1]


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
        assert span(build(conn, rows, first)) == [7, 7]
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
        # not there, so it compares weeks.
        assert span(build(conn, rows, AS_OF + 2 * DAY, minimum=20)) == [7, 7]


def test_each_network_compares_the_longest_periods_its_own_history_allows(database):
    young = days(dec(15) + 10 * DAY, 13, 1000) + days(dec(15) + 23 * DAY, 7, 1100)
    new = days(dec(15) + 27 * DAY, 2, 1000) + days(dec(15) + 29 * DAY, 1, 1100)
    with connect(database) as conn:
        build(conn, history(), dec(14))
        since = AS_OF - 30 * DAY
        windows = {}
        for name, rows in (("p", history()), ("q", young), ("r", new)):
            own = [r for r in rows if since <= r["bucket_start"] < AS_OF]
            windows[name] = {"_latency_days": day_records(own, TARGET, since, AS_OF, 30)}
        entries = [{"provider": n, "kind": "k", "windows": {"30d": w}} for n, w in windows.items()]
        complete(conn, entries, AS_OF)
    found = {n: w["latency_day_delta"] for n, w in windows.items()}
    assert {n: span(f) for n, f in found.items()} == {"p": [30, 30], "q": [7, 7], "r": [1, 1]}
    assert all(f["pct"] == pytest.approx(10.0) for f in found.values())
