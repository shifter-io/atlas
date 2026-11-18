"""OUTAGE-START-1: a pulse outage starts when the network's last healthy observation in the
country finished, a pulse of any target or a daily test with a success; when nothing at all was
observed there for more than a pulse interval after it, it starts with the first failing
observation instead, so days without pulses never count as the outage (outages.py). Fixtures
only; the database is the test's own."""

from __future__ import annotations

from datetime import timedelta

import pytest
from calculated_pulse_cases import SETTINGS, T0, after, pulses
from census_pulse_cases import product

from atlas.daily import DAILY, MARKER
from atlas.detect import detect, timestamp
from atlas.storage import connect, seed

pytest_plugins = ["test_storage"]

FAILED = 100  # the failing pulse: T0 + 50 hours, two days after the last healthy one (5)


def _daily(conn, product_id, at, successes, country="DE"):
    """A completed daily-test run of the network in the country (the atlas.daily contract:
    MARKER in its summary), started at `at`, 30 seconds long."""
    pool = conn.execute(
        "INSERT INTO pool_stats(product_id,pool_hash,target_id,requested_country) "
        "VALUES (%s,gen_random_uuid()::text,'generic_http',%s) RETURNING pool_id",
        (product_id, country.lower()),
    ).fetchone()[0]
    scope = conn.execute(
        "INSERT INTO scope_stats(pool_id,product_id,target,scope_hash,concurrency,"
        "timeout_seconds) VALUES (%s,%s,'generic_http',gen_random_uuid()::text,1,10) "
        "RETURNING scope_id",
        (pool, product_id),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO test_runs(run_id,scope_id,started_at,finished_at,status,summary_sha256,"
        "summary) VALUES (gen_random_uuid(),%s,%s,%s,'completed','0',"
        "jsonb_build_object(%s::text,%s::text,'successes',%s::bigint))",
        (scope, at, at + timedelta(seconds=30), MARKER, DAILY, successes),
    )


def _opened(conn):
    """Each outage's start, and the since of each 'down' on the tape."""
    starts = [row[0] for row in conn.execute("SELECT started_at FROM outages ORDER BY 1")]
    since = [
        row[0] for row in conn.execute("SELECT data->>'since' FROM test_events WHERE kind='down'")
    ]
    return starts, since


def _scenario(conn, dailies, *, interval=60):
    """Healthy pulses 0 to 5, then none in the country until pulse FAILED gets nothing; the
    daily tests in between, (hours after T0, successes); the outage detect opens."""
    conn.execute("UPDATE targets SET pulse_interval_minutes=%s", (interval,))
    alpha = product(conn)
    pulses(conn, alpha, [95] * 6)
    for hours, successes in dailies:
        _daily(conn, alpha, T0 + timedelta(hours=hours), successes)
    pulses(conn, alpha, [0], start=FAILED)
    detect(conn, after(FAILED), config=SETTINGS)
    return _opened(conn)


def test_a_healthy_daily_test_in_the_gap_is_the_last_healthy_observation(database):
    """The issue's case: no pulses for two days, the daily tests got through (the last one an
    hour before the failing pulse, within a pulse interval): the outage starts when that daily
    test finished, never at the pulse two days earlier."""
    seed(database)
    with connect(database) as conn:
        starts, since = _scenario(conn, [(25, 61), (49, 49)])
        latest = T0 + timedelta(hours=49, seconds=30)
        assert starts == [latest]
        assert since == [timestamp(latest)]


@pytest.mark.parametrize("dailies", [[], [(25, 61)]], ids=["no-daily", "daily-a-day-before"])
def test_hours_without_any_observation_are_not_the_outage(database, dailies):
    """Nothing at all observed for more than a pulse interval before the failing pulse: the
    outage starts with that pulse, not at the last healthy observation."""
    seed(database)
    with connect(database) as conn:
        starts, since = _scenario(conn, dailies)
        failed = T0 + timedelta(minutes=30 * FAILED)
        assert starts == [failed]
        assert since == [timestamp(failed)]


def test_a_failing_daily_test_after_the_gap_starts_the_outage(database):
    """The first failing observation after the gap is a daily test: the outage starts there."""
    seed(database)
    with connect(database) as conn:
        starts, _ = _scenario(conn, [(26, 0)])
        assert starts == [T0 + timedelta(hours=26)]


def test_a_failure_within_a_pulse_interval_still_starts_at_the_last_healthy_one(database):
    """Normal outages do not change: the next slot's pulse fails, so the outage starts when the
    last healthy pulse finished (5 finished a minute after its start)."""
    seed(database)
    with connect(database) as conn:
        alpha = product(conn)
        pulses(conn, alpha, [95] * 6 + [0])
        detect(conn, after(6), config=SETTINGS)
        assert _opened(conn)[0] == [T0 + timedelta(minutes=30 * 5 + 1)]
