"""CALC-SCALE-2: a verdict imported after the hourly build folded the minutes it rules on (each
hour's daily tests are) keeps the `*` IP memos and the IP state unless a run folded then stops
counting (fleet_verdicts.ruled()); the incremental build equals the full one either way. Before,
every such verdict emptied them, so every hourly build folded the whole window again.

CALC-PERF-1: when a run folded then stops counting, the latest build takes back only what it
added (fleet_verdicts.unfold()): it folds only that run's buckets again and moves the vectors,
and its memos and IP state equal those of a build that emptied them and counted everything again.
"""

import re
from datetime import UTC, datetime, timedelta

import pytest
from test_calculated_country_verdicts import completed, ruled_policy, verdict
from test_calculated_hourly_reads import HOUR, floor_hour
from test_calculated_incremental import assert_equal, history

from atlas.calculated import fleet_verdicts
from atlas.calculated import target_scores as ts
from atlas.calculated.calculator import calculate
from atlas.storage import connect

pytest_plugins = ["test_storage"]
pytestmark = pytest.mark.country_verdicts

T = datetime(2026, 11, 16, 12, tzinfo=UTC)
NEW = 10**9  # run keys of verdicts imported after a fold: newer than every run
# The IP memos and the IP state, as the next build reads them (vectors of 0 IPs are tidied).
MEMOS = (
    "SELECT bucket_start,runs_digest,unique_ips,scopes_digest FROM calculated_bucket_ips",
    "SELECT ip_id,last_seen FROM calculated_ip_last_seen",
    "SELECT ip_id,scope,last_seen FROM calculated_scope_ip_last_seen",
    "SELECT window_hours,scopes,ips FROM calculated_ip_vectors WHERE ips<>0",
    "SELECT as_of,windows,unbatched FROM calculated_ip_vector_state",
)


def ip_state(err):
    """(buckets, moved) of the last ip_state timing line in err."""
    found = re.findall(r"timing: calculate ip_state \S+ s buckets=(\d+) moved=(\d)", err)
    return tuple(map(int, found[-1]))


def memos(database):
    with connect(database) as conn:
        return [sorted(conn.execute(query).fetchall()) for query in MEMOS]


def unfolded(err):
    """(stopped runs, touched pairs) of the ip_unfold timing lines in err."""
    found = re.findall(r"timing: calculate ip_unfold \S+ s stopped=(\d+) pairs=(\d+)", err)
    return [tuple(map(int, f)) for f in found]


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
    # Which runs stopped: run 2, none, or not known (any may have).
    assert stopped.stopped_runs(state, late, until) == [2]
    assert same.stopped_runs(state, late, until) == []
    assert same.stopped_runs(state, T - 2 * fleet_verdicts.RECORDED, until) is None


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
    # when it counted, stops counting: only what it added is taken back and the vectors moved
    # (before CALC-PERF-1 the memos were emptied). Its buckets had no other run: none to fold.
    verdict(database, "alpha", "DE", end - 4 * HOUR, 3, NEW + 3)
    assert_equal(database, policy, end)
    assert_unfolded(database, policy, end, capsys, 1)
    # alpha US does not count from 22.5 hours back (3 IPs) until 5 hours back: its daily test
    # of 22 hours back stops; the one of 23.5 hours back, in the same 24-hour slice, still counts.
    verdict(database, "alpha", "US", end - 22 * HOUR - HOUR / 2, 3, NEW + 4)
    assert_equal(database, policy, end)
    assert_unfolded(database, policy, end, capsys, 1)


def assert_unfolded(database, policy, end, capsys, runs):
    """The build at end took back only what `runs` stopped runs added (fleet_verdicts.unfold()),
    and left the memos and IP state as a build that empties them and counts everything again
    does (a recount: the hourly build of the same inputs is not reused)."""
    err = capsys.readouterr().err
    assert ip_state(err)[1] == 1
    [(stopped, pairs)] = unfolded(err)
    assert stopped == runs and pairs > 0
    kept = memos(database)
    with connect(database) as conn:
        for statement in fleet_verdicts.EMPTY:
            conn.execute(statement)
    calculate(database, policy, as_of=end, mode="recount")
    assert ip_state(capsys.readouterr().err)[1] == 0
    assert memos(database) == kept
