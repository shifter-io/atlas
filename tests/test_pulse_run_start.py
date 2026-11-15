"""PULSE-STATE-2 (#1169 items 1 and 2): a moved pulse run has its own start (batch_runs.start_at,
schema v18); its batch, shared by the region's other networks, keeps its start. The pulse
evaluator, the claim and the reaper read each run by its own start."""

from __future__ import annotations

from datetime import timedelta

from census_pulse_cases import batch, batch_run
from orchestrator_cases import prepare
from pulse_cases import W0, buckets, products, window
from test_storage import measurement

from atlas.orchestrator import nodework, pulse
from atlas.orchestrator.nodes import issue_token
from atlas.storage import connect, ingest

pytest_plugins = ["test_storage", "orchestrator_cases"]

CFG = {"lookback": timedelta(minutes=13), "verdicts": False}


async def shared_batch(tmp_path, database):
    """One DE batch at W0: alpha ran on time (buckets in window 0); beta's run was moved from a
    stalled node and ran half an hour later (its own start, buckets in window 6)."""
    prepare(database, ("DE",))
    alpha, beta = products(database, "alpha", "beta")
    keys = []
    for name in ("alpha", "beta"):
        directory = await measurement(tmp_path / name)
        keys.append(ingest(directory, database)["run_key"])
    with connect(database) as conn:
        buckets(conn, keys[0], [(window(0), 100, 10, 300)])
        buckets(conn, keys[1], [(window(6), 100, 90, 300)])
        batch_id = batch(conn, "pulse", W0, country="DE")
        batch_run(conn, batch_id, alpha, W0 + timedelta(minutes=1), run_key=keys[0])
        batch_run(conn, batch_id, beta, window(6) + timedelta(minutes=1), run_key=keys[1])
        conn.execute(
            "UPDATE batch_runs SET start_at=%s WHERE batch_id=%s AND product_id=%s",
            (window(6), batch_id, beta),
        )
    return alpha, beta


async def test_each_run_counts_in_the_window_it_ran_in(tmp_path, database, db):
    alpha, beta = await shared_batch(tmp_path, database)
    async with db.connection() as conn:
        first = await pulse.window_stats(conn, window(0), 0.5, CFG)
        moved = await pulse.window_stats(conn, window(6), 0.5, CFG)
    assert set(first) == {alpha} and first[alpha]["errors"] == 10  # its own window, kept
    assert set(moved) == {beta} and moved[beta]["errors"] == 90  # where the moved run ran


async def test_a_moved_run_is_handed_out_and_reaped_by_its_own_start(database, db):
    prepare(database, ("DE",))
    [alpha] = products(database, "alpha")
    token = issue_token(database, provider="alpha", product="residential", region="fra1")
    node_id = token["node_id"]
    now = W0 + timedelta(minutes=10)
    with connect(database) as conn:
        batch_id = batch(conn, "pulse", W0, country="DE", status="running")
        conn.execute(
            "INSERT INTO batch_runs(batch_id,product_id,node_id,status,start_at) "
            "VALUES (%s,%s,%s,'queued',%s)",
            (batch_id, alpha, node_id, now + timedelta(seconds=30)),
        )
    node = {"node_id": node_id}
    [work] = (await nodework.assignments(db, node, now))["assignments"]
    assert work["start_at"] == (now + timedelta(seconds=30)).isoformat()
    # Its batch began 10 minutes ago, but the run's own start is ahead: not reaped as late.
    assert (await nodework.reap(db, now))["failed"] == {}
    later = now + timedelta(minutes=5)  # past its own start and the start grace (180 s)
    assert (await nodework.reap(db, later))["failed"] == {"not_started": 1}
