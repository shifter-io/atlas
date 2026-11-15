"""CALC-SCALE-1 (#1125): each network's unique IPs per closed hour, UTC day, rolling 30 days and
calendar month, stored once by the latest build (calculated/periods.py; the owner's Q9 option 1,
the website's pool.rolling30d and pool.months). Over builds at an hour before midnight, at
midnight (a day's close) and an hour later, each stored value equals the network's distinct IPs
counted straight from ip_observations, and every build equals the full calculation.
"""

from datetime import UTC, datetime, timedelta

import pytest
from calculated_cases import lenient_policy
from test_calc_fleet import fleet_history
from test_calculated_hourly_reads import DAY, HOUR, floor_hour
from test_calculated_incremental import assert_equal

from atlas.calculated import periods
from atlas.storage import connect

pytest_plugins = ["test_storage"]

# Each network's (provider, kind: the product's name with the lenient policy) distinct IPs in
# [start, end), per hour or day (trunc) or over the whole range (trunc NULL).
OBSERVED = """SELECT p.provider,p.name,
 coalesce(date_trunc(%(trunc)s,o.bucket_start,'UTC'),%(start)s),count(DISTINCT o.ip_id)
FROM ip_observations o JOIN test_runs r USING(run_key)
JOIN scope_details s USING(scope_id) JOIN products p ON p.product_id=s.product_id
WHERE o.bucket_start>=%(start)s AND o.bucket_start<%(end)s GROUP BY 1,2,3"""
STORED = """SELECT provider,kind,starts,unique_ips FROM calculated_unique_ips WHERE period=%s"""


def observed(database, start, end, trunc=None):
    with connect(database) as conn:
        rows = conn.execute(OBSERVED, {"trunc": trunc, "start": start, "end": end}).fetchall()
    return {(provider, kind, at): ips for provider, kind, at, ips in rows}


def stored(database, period):
    with connect(database) as conn:
        rows = conn.execute(STORED, (period,)).fetchall()
    return {(provider, kind, at): ips for provider, kind, at, ips in rows}


@pytest.mark.asyncio
async def test_the_stored_unique_ips_equal_the_observations(database, tmp_path):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await fleet_history(database, tmp_path, end)
    policy = lenient_policy(tmp_path)
    midnight = end.replace(hour=0)
    for as_of in (midnight - HOUR, midnight, midnight + HOUR):
        assert_equal(database, policy, as_of)
        kept = as_of - periods.KEPT
        hours = {k: v for k, v in stored(database, "hour").items() if k[2] >= kept}
        assert hours == observed(database, kept, as_of, "hour"), as_of
    days = {k: v for k, v in stored(database, "day").items() if k[2] >= midnight - 2 * DAY}
    assert days == observed(database, midnight - 2 * DAY, midnight, "day")
    rolling = stored(database, "rolling30d")
    assert rolling == {
        (p, k, midnight - DAY): ips
        for (p, k, _), ips in observed(database, midnight - 30 * DAY, midnight).items()
    }
    month = midnight - DAY
    month = month.replace(day=1)
    assert stored(database, "month") == observed(database, month, midnight)
    assert len(rolling) >= 3  # the fixture's networks


def test_ranks_count_the_networks_of_the_kind_with_strictly_more():
    values = {("a", "res"): 5, ("b", "res"): 9, ("c", "res"): 5, ("d", "dc"): 3, ("e", "res"): 0}
    assert periods.ranks(values) == {
        ("a", "res"): (2, 3),
        ("b", "res"): (1, 3),
        ("c", "res"): (2, 3),
        ("d", "dc"): (1, 1),
        ("e", "res"): (None, None),
    }


def test_a_day_closes_only_at_midnight_utc():
    at = datetime(2026, 11, 15, 0, tzinfo=UTC)
    assert periods._day(at + timedelta(hours=5)) == at
