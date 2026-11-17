"""CALC-SCALE-1 (#1125): counts across product groups from the per-scope IP state
(calculated/ipstate.py).

The latest build folds its changed buckets into calculated_scope_ip_last_seen and moves
calculated_ip_vectors to its cutoff through the IPs that changed. Over a sweep of cutoffs (an
hour on, a gap of several hours, a late run, an older cutoff between them, a run outside a batch
linked to one, which moves it to another scope) every build equals the
full calculation, which counts IP sets in Python (the cohorts' pairs and overlaps, the kinds'
distinct IPs), and after each latest build the vectors equal each window's IPs counted by their
scopes straight from ip_observations. An older cutoff leaves the state as it is; a state gone
wrong is counted again; sightings older than 31 days are collected.
"""

from datetime import UTC, datetime

import pytest
from calculated_cases import lenient_policy
from test_calc_fleet import fleet_history, late_run
from test_calculated_hourly_reads import DAY, HOUR, floor_hour
from test_calculated_incremental import assert_equal

from atlas.calculated import ipstate
from atlas.storage import connect

pytest_plugins = ["test_storage"]

WINDOWS = [24, 168, 720]
STATE_WINDOWS = sorted([ipstate.MONTH, *WINDOWS])  # the state also counts the month to date
STATE = ("calculated_scope_ip_last_seen", "calculated_ip_vectors", "calculated_ip_vector_state")
SCOPE = "r.scope_id::bigint*2+(r.batch_id IS NOT NULL)::int"
# Each window's IPs by the scopes that saw them in it, from the observations.
OBSERVED = f"""SELECT w.hours,x.scopes,count(*)
FROM unnest(%(hours)s::int[],%(since)s::timestamptz[]) AS w(hours,since)
CROSS JOIN LATERAL (SELECT o.ip_id,array_agg(DISTINCT {SCOPE} ORDER BY {SCOPE}) AS scopes
 FROM ip_observations o JOIN test_runs r USING(run_key)
 WHERE o.bucket_start>=w.since AND o.bucket_start<%(as_of)s
 GROUP BY 1) x GROUP BY 1,2"""


async def history(database, tmp_path, end):
    """test_calc_fleet's networks in five countries (IPs shared across groups), with pulse and
    census runs: scopes of runs in a batch, and a census-pulse cohort of two networks."""
    h = await fleet_history(database, tmp_path, end)
    await h.scheduled("pulse", "alpha", 26 * HOUR)
    await h.scheduled("pulse", "beta", 21 * HOUR)
    await h.scheduled("census", "gamma", 28 * HOUR)
    return h


def observed(database, as_of):
    with connect(database) as conn:
        bounds = {"hours": STATE_WINDOWS, "since": ipstate.starts(as_of, STATE_WINDOWS)}
        rows = conn.execute(OBSERVED, bounds | {"as_of": as_of}).fetchall()
    return {(hours, tuple(scopes)): ips for hours, scopes, ips in rows}


def stored(database):
    with connect(database) as conn:
        rows = conn.execute("SELECT window_hours,scopes,ips FROM calculated_ip_vectors")
        vectors = {(hours, tuple(scopes)): ips for hours, scopes, ips in rows}
        state = conn.execute("SELECT as_of,windows FROM calculated_ip_vector_state").fetchall()
    return vectors, [tuple(row) for row in state]


def state_rows(database):
    with connect(database) as conn:
        return {t: sorted(conn.execute(f"SELECT * FROM {t}").fetchall()) for t in STATE}


def link(database, before):
    """A run outside a batch with observations, started before then, joins the latest batch
    (storage_batch.link() on a run imported again with its batch)."""
    with connect(database) as conn:
        batch = conn.execute("SELECT max(batch_id) FROM test_batches").fetchone()[0]
        run = conn.execute(
            "SELECT min(r.run_key) FROM test_runs r WHERE r.batch_id IS NULL AND r.started_at<%s "
            "AND EXISTS (SELECT FROM ip_observations o WHERE o.run_key=r.run_key)",
            (before,),
        ).fetchone()[0]
        conn.execute("UPDATE test_runs SET batch_id=%s WHERE run_key=%s", (batch, run))


def midnights(after, upto):
    """The UTC midnights in (after, upto]: a build moves the IP state to each of them first
    (unique_ips.py closes the day there), so each is the cutoff its next move starts from."""
    day, found = upto.replace(hour=0), []
    while after is not None and day > after:
        found.insert(0, day)
        day -= DAY
    return found


def moves(monkeypatch):
    """The cutoff each move of the vectors started from (None: counted again)."""
    found, original = [], ipstate._move

    def move(cursor, as_of, hours, fold, before):
        found.append(before)
        return original(cursor, as_of, hours, fold, before)

    monkeypatch.setattr(ipstate, "_move", move)
    return found


@pytest.mark.asyncio
async def test_the_state_follows_the_cutoffs_and_every_build_equals_full(
    database, tmp_path, monkeypatch
):
    end = floor_hour(datetime.now(UTC)) + HOUR
    h = await history(database, tmp_path, end)
    policy = lenient_policy(tmp_path)
    found, pairs, original = moves(monkeypatch), [], ipstate.pair_counts
    monkeypatch.setattr(ipstate, "pair_counts", lambda *a: pairs.append(1) or original(*a))
    previous = state_at = None  # the first move's start; the state's cutoff
    for back in (30, 29, 24, "late", 20, "older", "link", 19, 18):
        if back == "late":
            await late_run(h)  # 40 h back: before every folded bucket's newest
            continue
        if back == "older":  # before the folded buckets: counted directly, the state kept
            kept = state_rows(database)
            assert_equal(database, policy, end - 27 * HOUR)
            assert state_rows(database) == kept
            continue
        if back == "link":  # its scope's sightings and the vectors are counted again
            link(database, end - 20 * HOUR)
            previous = None
            continue
        as_of, start = end - back * HOUR, len(found)
        assert_equal(database, policy, as_of)
        vectors, state = stored(database)
        assert vectors == observed(database, as_of), back
        assert [row[:2] for row in state] == [(as_of, STATE_WINDOWS)]
        # The first build counts, the others move (through each midnight passed).
        assert found[start:] == [previous, *midnights(state_at, as_of)], back
        previous = state_at = as_of
    assert found[0] is None
    assert pairs  # the cohorts of two or more were summed from the vectors
    shared = {k for k in observed(database, end - 18 * HOUR) if len(k[1]) > 1}
    assert {n for n, _ in shared} >= set(WINDOWS)  # IPs that two scopes saw, in every window
    assert any(s % 2 for _, scopes in shared for s in scopes)  # a scope of runs in a batch


@pytest.mark.asyncio
async def test_a_wrong_state_is_counted_again_and_old_sightings_are_collected(
    database, tmp_path, monkeypatch
):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await history(database, tmp_path, end)
    policy = lenient_policy(tmp_path)
    assert_equal(database, policy, end - 2 * HOUR)
    with connect(database) as conn:
        conn.execute("UPDATE calculated_ip_vectors SET ips=ips-1000 WHERE window_hours=720")
        conn.execute(
            "INSERT INTO calculated_scope_ip_last_seen VALUES (-1,7,%s)", (end - 40 * DAY,)
        )
    found = moves(monkeypatch)
    assert_equal(database, policy, end)
    # Moved, gone negative, then counted again (then moved on from each midnight passed).
    assert found == [end - 2 * HOUR, None, *midnights(end - 2 * HOUR, end)]
    assert stored(database) == (observed(database, end), [(end, STATE_WINDOWS)])
    with connect(database) as conn:
        query = "SELECT count(*) FROM calculated_scope_ip_last_seen WHERE ip_id=-1"
        assert conn.execute(query).fetchone() == (0,)


@pytest.mark.asyncio
async def test_other_windows_count_the_state_again(database, tmp_path, monkeypatch):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await history(database, tmp_path, end)
    assert_equal(database, lenient_policy(tmp_path), end - HOUR)
    with connect(database) as conn:
        conn.execute("UPDATE calculated_ip_vector_state SET windows='{24,168}'")
    found = moves(monkeypatch)
    assert_equal(database, lenient_policy(tmp_path), end)
    assert found == [None, *midnights(end - HOUR, end)]
    assert stored(database) == (observed(database, end), [(end, STATE_WINDOWS)])


def test_a_cohorts_pairs_are_sums_over_its_vectors():
    """Two networks of one provider and one of another: per pair the IPs both saw, those a
    member of another provider saw too, and the distinct IPs; scopes of other groups ignored."""
    vectors = ipstate.Vectors([((1,), 5), ((1, 2), 3), ((2, 3), 2), ((3, 9), 4), ((9,), 7)])
    members = [({"provider": "a"}, [10]), ({"provider": "a"}, [20]), ({"provider": "b"}, [30])]
    scope_of = {10: 1, 20: 2, 30: 3}
    assert ipstate.pair_counts(vectors, members, scope_of) == {
        (0, 0): (8, 0),
        (0, 1): (3, 0),
        (1, 1): (5, 2),
        (1, 2): (2, 2),
        (2, 2): (6, 2),
        (None, None): (14, 0),
    }
    assert vectors.distinct({1, 3}) == 14 and vectors.distinct({8}) == 0
    assert ipstate.pair_counts(ipstate.Vectors([]), members, scope_of) == {}
