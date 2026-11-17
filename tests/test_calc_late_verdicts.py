"""CALC-SCALE-2: a verdict imported after the hourly build folded the minutes it rules on (each
hour's daily tests are) keeps the `*` IP memos and the IP state unless a run folded then stops
counting (fleet_verdicts.ruled()); the incremental build equals the full one either way. Before,
every such verdict emptied them, so every hourly build folded the whole window again.
"""

import re
from datetime import UTC, datetime, timedelta

import pytest
from test_calculated_country_verdicts import completed, ruled_policy, verdict
from test_calculated_hourly_reads import HOUR, floor_hour
from test_calculated_incremental import assert_equal, history

from atlas.calculated import fleet_verdicts
from atlas.calculated import target_scores as ts

pytest_plugins = ["test_storage"]
pytestmark = pytest.mark.country_verdicts

T = datetime(2026, 11, 16, 12, tzinfo=UTC)
NEW = 10**9  # run keys of verdicts imported after a fold: newer than every run


def ip_state(err):
    """(buckets, moved) of the last ip_state timing line in err."""
    found = re.findall(r"timing: calculate ip_state \S+ s buckets=(\d+) moved=(\d)", err)
    return tuple(map(int, found[-1]))


def counted(runs, rule=10):
    """Counted of runs [(run_key, started_at, counted)], without bucket rows."""
    rows = [{"run_key": k, "started_at": at, "counted": c} for k, at, c in runs]
    return fleet_verdicts.Counted([], rule, rows)


def test_a_fold_records_the_runs_of_its_last_day_that_did_not_count():
    old = T - timedelta(days=2)
    found = counted([(1, old, False), (2, T - HOUR, False), (3, T - HOUR, True), (4, T, False)])
    since = (T - fleet_verdicts.RECORDED).isoformat()
    assert found.record(T) == {"since": since, "newest": 4, "excluded": [2, 4]}
    assert counted([]).record(T) == {"since": since, "newest": None, "excluded": []}


def test_only_a_folded_run_that_stopped_counting_empties_the_memos():
    state = counted([(1, T - 3 * HOUR, False), (2, T - 2 * HOUR, True)]).record(T)
    late, until = T - 4 * HOUR, T  # the oldest late verdict, the folded buckets' end
    # The same runs, and a new run that does not count (it was never folded): nothing stopped.
    same = counted([(1, T - 3 * HOUR, False), (2, T - 2 * HOUR, True), (3, T - HOUR, False)])
    assert not same.stopped(state, late, until)
    # Run 2 counted when it was folded and does not now.
    stopped = counted([(1, T - 3 * HOUR, False), (2, T - 2 * HOUR, False)])
    assert stopped.stopped(state, late, until)
    # ... unless it started after the folded buckets end (its buckets were not folded).
    assert not stopped.stopped(state, late, T - 2 * HOUR)
    # A late verdict older than the recorded day, or a fold that recorded nothing: emptied.
    assert same.stopped(state, T - 2 * fleet_verdicts.RECORDED, until)
    assert same.stopped({"on": True, "through": 0}, late, until)


@pytest.mark.asyncio
async def test_late_verdicts_keep_the_ip_memos_unless_a_folded_run_stops_counting(
    database, tmp_path, monkeypatch, capsys
):
    end = floor_hour(datetime.now(UTC)) + HOUR
    monkeypatch.setattr(ts, "MIN_REQUESTS", 1)  # a run has 40 requests
    await history(database, tmp_path, end)
    # As test_calculated_country_verdicts: alpha US counts at 70 and 22 hours back, alpha DE
    # from 60 hours back (its runs of 50 and 3 hours back), beta US (9 IPs) never.
    for n, (back, ips) in enumerate(((71, 40), (47, 6), (23, 12)), 1):
        verdict(database, "alpha", "US", end - back * HOUR, ips, -n)
    verdict(database, "alpha", "DE", end - 60 * HOUR, 45, -4)
    verdict(database, "beta", "US", end - 60 * HOUR, 9, -5)
    policy = ruled_policy(tmp_path)
    assert_equal(database, policy, end)
    assert ip_state(capsys.readouterr().err)[1] == 0  # the first fold counts everything
    # Imported now, decided before the folded buckets end (alpha DE's daily test 3 hours back):
    # alpha US still counts (30 IPs).
    verdict(database, "alpha", "US", end - 5 * HOUR, 30, NEW + 1)
    assert_equal(database, policy, end)
    assert ip_state(capsys.readouterr().err) == (0, 1)  # kept: nothing to fold again
    # beta US counts from 23.5 hours back: its daily test 23 hours back is folded now, only its
    # buckets, and its 40 requests reach the figures.
    verdict(database, "beta", "US", end - 23 * HOUR - HOUR / 2, 40, NEW + 2)
    payload = assert_equal(database, policy, end)
    buckets, moved = ip_state(capsys.readouterr().err)
    assert moved == 1 and 0 < buckets < 12
    assert completed(payload, "beta", "US", "24h") == 40
    # alpha DE does not count from 4 hours back (3 IPs): its daily test 3 hours back, folded
    # when it counted, stops counting, so the memos are emptied and counted again.
    verdict(database, "alpha", "DE", end - 4 * HOUR, 3, NEW + 3)
    assert_equal(database, policy, end)
    assert ip_state(capsys.readouterr().err)[1] == 0
