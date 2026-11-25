"""PULSE-NODE-DNS-2: a pulse without success opens an outage and writes 'down' only when the
series' (product, country, target) previous judged pulse got no success either. A single one
waits for the next pulse and is at most a pulse_finished; a confirmed outage still starts when
the last healthy pulse finished (outages._outage_start), so it covers both pulses. The same for
every network. Fixtures only; the database is the test's own."""

from __future__ import annotations

from datetime import timedelta

import pytest
from calculated_pulse_cases import SETTINGS, T0, after, pulses
from census_pulse_cases import product
from test_pulse_no_exit import stored

from atlas.detect import detect, timestamp
from atlas.storage import connect, seed

pytest_plugins = ["test_storage"]

HEALTHY = [95] * 6  # pulses 0 to 5: the baseline (min_baseline_batches)
LAST_HEALTHY = T0 + timedelta(minutes=30 * 5 + 1)  # when pulse 5 finished


def _tape(conn):
    """(kind, signal, severity, data value) of each event, in order, and each outage's start and
    end."""
    events = conn.execute(
        "SELECT kind,signal,severity,(data->>'value')::float FROM test_events ORDER BY event_id"
    ).fetchall()
    outages = conn.execute("SELECT started_at,ended_at FROM outages ORDER BY 1").fetchall()
    return events, outages


def _step(conn, upto):
    """detect() after each pulse from 6 to upto, as the orchestrator runs it."""
    for i in range(6, upto + 1):
        detect(conn, after(i), config=SETTINGS)


def test_a_single_pulse_without_success_opens_nothing(database):
    """The failing pulse is a pulse_finished (down from the last pulse), not a 'down' nor a
    success_drop; no outage opens."""
    seed(database)
    with connect(database) as conn:
        pulses(conn, product(conn), [*HEALTHY, 0])
        _step(conn, 6)
        events, outages = _tape(conn)
        assert events == [("pulse_finished", -1, "info", 0.0)]
        assert outages == []


def test_the_next_pulse_without_success_confirms_from_the_last_healthy_one(database):
    """The second pulse in a row without success writes 'down' and opens the outage; both start
    when the last healthy pulse finished, before the first failing one."""
    seed(database)
    with connect(database) as conn:
        pulses(conn, product(conn), [*HEALTHY, 0, 0])
        _step(conn, 7)
        events, outages = _tape(conn)
        assert [e[0] for e in events] == ["pulse_finished", "down"]
        assert events[1][1:3] == (-1, "critical")
        assert outages == [(LAST_HEALTHY, None)]
        since = conn.execute("SELECT data->>'since',at FROM test_events WHERE kind='down'")
        assert since.fetchone() == (timestamp(LAST_HEALTHY), LAST_HEALTHY)


def test_a_failure_judged_together_with_its_confirmation(database):
    """Both failing pulses judged in one detect() call: the same 'down' and outage."""
    seed(database)
    with connect(database) as conn:
        pulses(conn, product(conn), [*HEALTHY, 0, 0])
        detect(conn, after(7), config=SETTINGS)
        events, outages = _tape(conn)
        assert [e[0] for e in events] == ["pulse_finished", "down"]
        assert outages == [(LAST_HEALTHY, None)]


def test_a_single_failure_followed_by_a_healthy_pulse_leaves_no_outage(database):
    """Fail, then through: two pulse_finished events, no 'down', no 'recovered', no outage."""
    seed(database)
    with connect(database) as conn:
        pulses(conn, product(conn), [*HEALTHY, 0, 95, 0])
        _step(conn, 8)
        events, outages = _tape(conn)
        assert [(e[0], e[1]) for e in events] == [
            ("pulse_finished", -1),
            ("pulse_finished", 1),
            ("pulse_finished", -1),
        ]
        assert outages == []


def test_an_open_down_needs_no_new_confirmation(database):
    """Once the series is 'down', each later pulse without success is still the same outage:
    no second 'down', and the first pulse through closes it with a 'recovered'."""
    seed(database)
    with connect(database) as conn:
        pulses(conn, product(conn), [*HEALTHY, 0, 0, 0, 95])
        _step(conn, 9)
        events, outages = _tape(conn)
        assert [e[0] for e in events] == ["pulse_finished", "down", "recovered"]
        assert outages == [(LAST_HEALTHY, T0 + timedelta(minutes=30 * 9))]


def test_another_targets_failure_is_no_confirmation(database):
    """The series is per target: one failing pulse on each target, in two slots, opens nothing."""
    seed(database)
    with connect(database) as conn:
        alpha = product(conn)
        pulses(conn, alpha, [*HEALTHY, 0, 95])
        pulses(conn, alpha, [*HEALTHY, 95, 0], target="generic_https")
        _step(conn, 7)
        events, outages = _tape(conn)
        assert "down" not in [e[0] for e in events]
        assert outages == []


async def test_a_pulse_refused_at_login_is_no_confirmation(database, tmp_path):
    """Pulse 6 was refused at login (not judged, PROXY-AUTH-ALERT-1): pulse 7 without success is
    a first failure, pulse 8 the 'down'; the outage starts at the last healthy pulse."""
    seed(database)
    refused, timeouts = {"407": 100, "proxy_auth": 100}, {"timeout": 100}
    keys = await stored(tmp_path, database, [refused, timeouts, timeouts])
    with connect(database) as conn:
        runs = dict(zip((6, 7, 8), keys, strict=True))
        pulses(conn, product(conn), [*HEALTHY, 0, 0, 0], run_keys=runs)
        _step(conn, 8)
        events, outages = _tape(conn)
        assert [e[0] for e in events] == ["pulse_finished", "down"]
        assert outages == [(LAST_HEALTHY, None)]


def _late(conn, target, i):
    """Pulse i on the target finishes 10 minutes later, so it is judged after the other one."""
    conn.execute(
        "UPDATE test_batches SET finished_at=finished_at+interval '10 minutes' "
        "WHERE target_id=%s AND scheduled_for=%s",
        (target, T0 + timedelta(minutes=30 * i)),
    )


@pytest.mark.parametrize("last", ["generic_http", "generic_https"])
def test_another_targets_first_failure_holds_the_outage(database, last):
    """HTTP fails at 6 and 7, HTTPS only at 7 and 8, HTTP still at 8. At 7 HTTPS's failure is
    its first: no outage, whichever target is judged last. At 8 both series are 'down' and the
    outage opens from HTTPS's last healthy pulse (6); HTTPS through at 9 ends it."""
    seed(database)
    with connect(database) as conn:
        alpha = product(conn)
        pulses(conn, alpha, [*HEALTHY, 0, 0, 0, 0])
        pulses(conn, alpha, [*HEALTHY, 95, 0, 0, 95], target="generic_https")
        _late(conn, last, 7)
        detect(conn, after(7) + timedelta(minutes=10), config=SETTINGS)
        assert _tape(conn)[1] == []
        _step(conn, 9)
        downs = conn.execute("SELECT target_id FROM test_events WHERE kind='down' ORDER BY 1")
        assert [row[0] for row in downs] == ["generic_http", "generic_https"]
        healthy = T0 + timedelta(minutes=30 * 6 + 1)
        assert _tape(conn)[1] == [(healthy, T0 + timedelta(minutes=30 * 9))]
