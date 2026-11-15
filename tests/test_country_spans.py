"""COUNTRY-MIN-IPS-1: the spans in which a network's country counts (verdicts.py), outages cut to
them and uptime over them, and the methodology's country_verdicts setting.

The owner's example: a daily test finds 100 IPs (the country counts), the next day's 6 (it does
not), the third day's 12 (it counts again); before the first daily test it does not count.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from census_pulse_cases import product

from atlas import verdicts
from atlas.calculated import policy
from atlas.calculated.outage_time import outage_uptime
from atlas.storage import connect

pytest_plugins = ["test_storage"]

POLICY = policy.load(Path(__file__).parents[1] / "configs/methodology.toml")[0]
DAY0 = datetime(2026, 11, 10, tzinfo=UTC)
HOUR, DAY = timedelta(hours=1), timedelta(days=1)


def at(day, hour=0):
    return DAY0 + day * DAY + hour * HOUR


# The owner's example in DE: 100 IPs on day 1 at 10:00, 6 on day 2, 12 on day 3.
EXAMPLE = ((at(1, 10), 100), (at(2, 10), 6), (at(3, 10), 12))
COUNTING = [(at(1, 10), at(2, 10)), (at(3, 10), at(4))]


def verdict(conn, product_id, country, decided, ips, key):
    conn.execute(
        "INSERT INTO country_verdicts(product_id,country_code,decided_at,run_key,unique_ips) "
        "VALUES (%s,%s,%s,%s,%s)",
        (product_id, country, decided, key, ips),
    )


def example(database):
    """Product alpha with the owner's example in DE, and beta with 9 IPs in DE on day 0."""
    with connect(database) as conn:
        alpha, beta = product(conn, "alpha"), product(conn, "beta")
        for n, (decided, ips) in enumerate(EXAMPLE, 1):
            verdict(conn, alpha, "DE", decided, ips, -n)
        verdict(conn, beta, "DE", at(0, 12), 9, -9)
    return alpha, beta


def test_the_counting_spans_follow_the_latest_daily_test(database):
    alpha, _ = example(database)
    with connect(database) as conn:
        found = verdicts.spans(conn, at(0), at(4))
    # Before day 1 (no verdict), on day 2 (6 IPs) and for beta (9 IPs) nothing counts.
    assert [(r["product_id"], r["country"], r["lo"], r["hi"]) for r in found] == [
        (alpha, "DE", lo, hi) for lo, hi in COUNTING
    ]
    with connect(database) as conn:  # a window inside day 2 sees no span; one on day 3 its own
        assert verdicts.spans(conn, at(2, 12), at(3)) == []
        assert verdicts.spans(conn, at(3, 12), at(5))[0]["lo"] == at(3, 12)


def test_overlap_keeps_the_parts_inside_the_spans():
    spans = [(at(3), at(4)), (at(1), at(2)), (at(1, 12), at(2, 6))]  # unsorted, overlapping
    assert verdicts.overlap([(at(0), at(5))], spans) == [(at(1), at(2, 6)), (at(3), at(4))]
    assert verdicts.overlap([(at(2, 7), at(2, 9))], spans) == []
    assert verdicts.overlap([(at(0), at(1))], spans) == []  # touching is not overlapping


def test_an_outage_is_cut_to_the_time_its_country_counted():
    counting = {7: {"DE": COUNTING}}
    open_one = {"product_id": 7, "country": "DE ", "started_at": at(1, 20), "ended_at": None}
    found = verdicts.clip([open_one], counting, at(4))
    # Day 2 from 10:00 to day 3 10:00 is left out; the part reaching the cutoff is still open.
    assert [(o["started_at"], o["ended_at"]) for o in found] == [
        (at(1, 20), at(2, 10)),
        (at(3, 10), None),
    ]
    closed = open_one | {"ended_at": at(3, 12)}
    found = verdicts.clip([closed], counting, at(4))
    assert [(o["started_at"], o["ended_at"]) for o in found] == [
        (at(1, 20), at(2, 10)),
        (at(3, 10), at(3, 12)),
    ]
    # Another product or country has no span: the outage is not in any figure.
    assert verdicts.clip([open_one | {"product_id": 8}], counting, at(4)) == []
    assert verdicts.clip([open_one | {"country": "FR"}], counting, at(4)) == []


def test_uptime_counts_only_the_time_the_country_counted():
    pulses = [{"country": "DE", "started_at": at(0, 1), "last": at(4)}]
    tested = {"DE": at(4)}
    down = [{"country": "DE", "started_at": at(1, 22), "ended_at": at(2)}]  # 2 hours, counted
    found = outage_uptime(down, tested, at(0), at(4), pulses, {"DE": COUNTING})
    counted = 24 * 3600 + 14 * 3600  # day 1 10:00 to day 2 10:00, day 3 10:00 to day 4 00:00
    assert found["value"] == pytest.approx(1 - 2 * 3600 / counted)
    assert found["countries"] == 1
    # The measured span never bridges day 2, which did not count; the window basis neither.
    window = outage_uptime(down, tested, at(0), at(4), None, {"DE": COUNTING})
    assert window["value"] == found["value"]
    # A country that never counted has no time: no value.
    assert outage_uptime(down, tested, at(0), at(4), pulses, {})["value"] is None
    # Without counting spans uptime is as before (the methodology's setting off).
    assert outage_uptime(down, tested, at(0), at(4), pulses)["value"] == pytest.approx(
        1 - 2 / (4 * 24 - 1)
    )


def test_the_methodology_counts_daily_tested_countries_only():
    assert POLICY["country_verdicts"] is True
    assert verdicts.MIN_IPS == 10


@pytest.mark.country_verdicts
def test_the_setting_is_a_boolean_and_off_without_it():
    assert policy.country_verdicts(POLICY) is True
    assert policy.country_verdicts({}) is False
    for value in (1, "true", None):
        with pytest.raises(ValueError, match="Invalid country_verdicts"):
            policy.validate(POLICY | {"country_verdicts": value})
