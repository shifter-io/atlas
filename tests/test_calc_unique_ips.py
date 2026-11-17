"""CALC-SCALE-1 (#1125): each network's unique IPs per closed hour, UTC day, rolling 30 days and
calendar month, stored once by the latest build (calculated/unique_ips.py; the owner's Q9 option 1,
the website's pool.rolling30d and pool.months). Over builds at production's cutoffs, a few
minutes past the hour (before midnight, the first after it: a day's close, and the next), each
stored value equals the network's distinct IPs counted straight from ip_observations over whole
hours, days and months, and every build equals the full calculation.
"""

from datetime import UTC, datetime, timedelta

import pytest
from calculated_cases import lenient_policy
from test_calc_fleet import fleet_history
from test_calc_ipstate import observed as ipstate_observed
from test_calc_ipstate import stored as ipstate_stored
from test_calculated_hourly_reads import DAY, HOUR, PRODUCT_IPS, floor_hour
from test_calculated_incremental import assert_equal

from atlas.calculated import ipstate, unique_ips
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
MINUTE = timedelta(minutes=1)


def observed(database, start, end, trunc=None):
    with connect(database) as conn:
        rows = conn.execute(OBSERVED, {"trunc": trunc, "start": start, "end": end}).fetchall()
    return {(provider, kind, at): ips for provider, kind, at, ips in rows}


def stored(database, period):
    with connect(database) as conn:
        rows = conn.execute(STORED, (period,)).fetchall()
    return {(provider, kind, at): ips for provider, kind, at, ips in rows}


def fresh_ip(database, run_key, at):
    """Let the run see, at at, an IP no run saw before, labelled as an IP it saw."""
    with connect(database) as conn:
        ip = conn.execute("INSERT INTO ip_addresses(ip) VALUES ('203.0.113.9') RETURNING ip_id")
        ip = ip.fetchone()[0]
        seen = conn.execute("SELECT min(ip_id) FROM ip_observations WHERE run_key=%s", (run_key,))
        columns = ",".join(c for (c,) in conn.execute(COLUMNS) if c != "ip_id")
        conn.execute(
            f"INSERT INTO ip_enrichment(ip_id,{columns}) SELECT %s,{columns} FROM ip_enrichment "
            "WHERE ip_id=%s",
            (ip, seen.fetchone()[0]),
        )
        conn.execute(
            "INSERT INTO ip_observations VALUES (%s,%s,%s,1,%s,%s)",
            (at, run_key, ip, at, at + MINUTE),
        )
        conn.execute(PRODUCT_IPS)


COLUMNS = """SELECT column_name FROM information_schema.columns
WHERE table_name='ip_enrichment' ORDER BY ordinal_position"""


def month_first():
    """The most recent 1st of a month at 06:00 UTC that is past: its midnight ends a month."""
    now = datetime.now(UTC)
    first = now.replace(day=1, hour=6, minute=0, second=0, microsecond=0)
    return first if first <= now else (first - DAY).replace(day=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("at", ["now", "month"])
async def test_the_stored_unique_ips_equal_the_observations(database, tmp_path, at):
    """The review's regression: production's cutoffs are a few minutes past the hour, so no
    rolling or month row was written and the hour rows started at the cutoff's minutes. The
    month to date comes from the IP state's month window (CALC-SCALE-3); at a month's first
    midnight ("month") the close counts the month that ends there and the next build moves the
    window to the new month, every vector still equal to the observations."""
    end = floor_hour(datetime.now(UTC)) + HOUR if at == "now" else month_first()
    h = await fleet_history(database, tmp_path, end)
    midnight = end.replace(hour=0)
    # A test at 00:00 sees an IP new to its network: the day closed at midnight leaves it out.
    fresh_ip(database, await h.daily("alpha", "us", end - midnight), midnight)
    policy = lenient_policy(tmp_path)
    for as_of in (midnight - 55 * MINUTE, midnight + 5 * MINUTE, midnight + 10 * MINUTE):
        assert_equal(database, policy, as_of)
        assert ipstate_stored(database)[0] == ipstate_observed(database, as_of), as_of
        hour = floor_hour(as_of)
        found = stored(database, "hour")
        assert all(k[2] == floor_hour(k[2]) for k in found), as_of  # clock hours only
        kept = {k: v for k, v in found.items() if hour - unique_ips.KEPT <= k[2] < hour}
        assert kept == observed(database, hour - unique_ips.KEPT, hour, "hour"), as_of
        assert all(k[2] < hour for k in found), as_of  # no row of the hour still open
        if as_of == midnight + 5 * MINUTE:
            with connect(database) as conn:
                closed = conn.execute(CLOSED).fetchall()
    with connect(database) as conn:
        assert conn.execute(CLOSED).fetchall() == closed  # closed once, not again at 00:10
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


CLOSED = """SELECT provider,kind,period,starts,computed_at FROM calculated_unique_ips
WHERE period IN ('rolling30d','month') ORDER BY 1,2,3,4"""


def test_ranks_count_the_networks_of_the_kind_with_strictly_more():
    values = {("a", "res"): 5, ("b", "res"): 9, ("c", "res"): 5, ("d", "dc"): 3, ("e", "res"): 0}
    assert unique_ips.ranks(values) == {
        ("a", "res"): (2, 3),
        ("b", "res"): (1, 3),
        ("c", "res"): (2, 3),
        ("d", "dc"): (1, 1),
        ("e", "res"): (None, None),
    }


def test_a_day_closes_only_at_midnight_utc():
    at = datetime(2026, 11, 15, 0, tzinfo=UTC)
    assert unique_ips._day(at + timedelta(hours=5)) == at


def test_the_month_window_starts_with_the_month_of_the_cutoffs_last_instant():
    first = datetime(2026, 12, 1, tzinfo=UTC)
    assert ipstate.month_start(first) == datetime(2026, 11, 1, tzinfo=UTC)  # its close: November
    assert ipstate.month_start(first + 5 * MINUTE) == first
    at = datetime(2026, 11, 17, 13, 5, tzinfo=UTC)
    assert ipstate.starts(at, [ipstate.MONTH, 24, 720]) == [
        datetime(2026, 11, 1, tzinfo=UTC),
        at - DAY,
        at - 30 * DAY,
    ]
