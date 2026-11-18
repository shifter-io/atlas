"""CALC-SCALE-1 (#1125): an incremental build's product groups measured in worker processes
(calculate.workers; calculated/parallel_measure.py) give the build one process gives: the
incremental build with workers equals the full calculation, as the one-process build does
(test_calculated_incremental), read back. Workers only read, each over its own connection in the
build's snapshot; the calculation writes their lists in one process's order.
"""

import asyncio
import gc
import json
import os
import signal
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from calculated_cases import lenient_policy
from test_calculated_hourly_reads import HOUR, floor_hour
from test_calculated_incremental import first_difference, history, without

from atlas.calculated import calculator, parallel_measure, store
from atlas.calculated.calculator import calculate
from atlas.orchestrator.control import write_settings
from atlas.storage import connect

pytest_plugins = ["test_storage"]


def equal_to_full(database, policy, as_of):
    """The incremental build (with the database's calculate.workers), then the full one: equal
    as read back."""
    new = calculate(database, policy, as_of=as_of, mode="incremental")
    full = calculate(database, policy, as_of=as_of, mode="full")
    a, b = (without(store.read(database, p["snapshot_id"])) for p in (new, full))
    assert json.dumps(a) == json.dumps(b), first_difference(a, b)
    return new


@pytest.mark.asyncio
@pytest.mark.parametrize("fork", [False, True])
async def test_workers_build_what_one_process_builds(fork, database, tmp_path, monkeypatch, capsys):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await history(database, tmp_path, end)
    policy = lenient_policy(tmp_path)
    monkeypatch.setattr(parallel_measure.Measure, "FORK", fork)
    with connect(database) as conn:
        write_settings(conn, {"calculate.workers": 3})
    for hours in (30, 0):  # the second build reuses some lists and counts the others
        payload = equal_to_full(database, policy, end - hours * HOUR)
    err = capsys.readouterr().err
    assert "calculate: 3 workers for 4 products" in err
    assert "calculate: 3 workers for 4 networks" in err  # the summaries too, equal as well
    for step in ("measured", "summarized"):  # each pool's private memory, a timing line
        assert f"s step={step} workers=3 private_mb=" in err
    with connect(database) as conn:  # every window names a list the build has
        found = conn.execute(
            "SELECT count(*) FILTER (WHERE l.list_id IS NULL) FROM calculated_windows w "
            "LEFT JOIN calculated_lists l USING(list_id) WHERE w.build_id=%s AND w.target=''",
            (int(payload["snapshot_id"]),),
        ).fetchone()
    assert found == (0,)


@pytest.mark.asyncio
async def test_rollups_recounted_in_the_build_are_measured_in_its_process(
    database, tmp_path, monkeypatch, capsys
):
    """A worker's snapshot lacks what the build writes itself: the geo rollup hours it recounts
    (all of them here, with nothing filled before the build) are read in its own process."""
    end = floor_hour(datetime.now(UTC)) + HOUR
    await history(database, tmp_path, end)
    policy = lenient_policy(tmp_path)
    monkeypatch.setattr(calculator.rollups, "fill", lambda database, as_of, rule=False: None)
    monkeypatch.setattr(parallel_measure.Measure, "FORK", False)
    with connect(database) as conn:
        write_settings(conn, {"calculate.workers": 3})
    equal_to_full(database, policy, end)
    assert "calculate: 1 process (" in capsys.readouterr().err


def test_the_workers_setting_is_a_whole_number_from_1_to_32():
    assert parallel_measure.workers(1) == 1 and parallel_measure.workers(32) == 32
    for wrong in (0, 33, "4", 2.0, None):
        with pytest.raises(ValueError, match="calculate.workers"):
            parallel_measure.workers(wrong)
    assert parallel_measure.measure(None, "full", 4) is None  # the oracle measures here
    assert parallel_measure.measure(None, "incremental", 1) is None


def test_a_product_is_a_task_and_its_lists_are_written_in_the_builds_order(monkeypatch):
    """_run() yields each task's result in the tasks' order (as the build writes their lists) and
    prints its pool's timing line; here in this process (FORK False)."""
    monkeypatch.setattr(parallel_measure.Measure, "FORK", False)
    measure = parallel_measure.Measure(None, 3)
    found = list(measure._run(lambda task: [k * 10 for k in task], [[1], [2, 3], [4]], 3))
    assert found == [[10], [20, 30], [40]]


def test_at_most_twice_the_workers_tasks_are_out_at_once():
    """_bounded(): the results in the tasks' order, never more than `most` tasks submitted and
    not yet taken back; and no task, no pool (no product or network: no Pool(0))."""

    class Pool:
        out = most = 0

        def apply_async(self, work, args):
            Pool.out += 1
            Pool.most = max(Pool.most, Pool.out)
            result = work(*args)

            def get():
                Pool.out -= 1
                return result

            return SimpleNamespace(get=get)

    found = list(parallel_measure._bounded(Pool(), lambda t: t * 2, range(10), 4))
    assert found == [t * 2 for t in range(10)] and Pool.most == 4
    assert list(parallel_measure._bounded(Pool(), lambda t: t, [], 4)) == []
    measure = parallel_measure.Measure(None, 3)
    step = SimpleNamespace(geo_groups=SimpleNamespace(geo=SimpleNamespace(recounted=[])))
    assert measure(step, {}, []) is None and measure.networks([], None) == []


def doubled(task):
    return [k * 2 for k in task]


def failed(task):
    raise ValueError(f"task {task}")


BUSY = {}  # the pipe a busy worker writes its process id to, made before the fork


def busy():
    os.write(BUSY["write"], f"{os.getpid()}\n".encode())
    time.sleep(60)


@pytest.mark.timeout(60)
@pytest.mark.parametrize("handler", ["asyncio", "ctrl-c"])
async def test_a_forked_worker_ends_on_a_sigterm_whatever_this_process_does_on_one(handler):
    """PUBLIC-CI-FORK-HANG-1: terminate() holds the workers' task queue, sends each worker left a
    SIGTERM and waits for it to end; a worker outlived the SIGTERM with this process's handler,
    asyncio's (which returns) or a script's Ctrl-C one (scripts/scale/simulate.py, run in a
    test's process before), and the build waited forever (pytest-timeout's 300 s on GitHub's
    runners). Now each worker ends on it, and no worker's signal reaches this process's loop (the
    wakeup fd it inherited)."""
    loop, seen = asyncio.get_running_loop(), []
    loop.add_signal_handler(signal.SIGTERM, seen.append, "SIGTERM")
    if handler == "ctrl-c":  # conftest.sigterm_restored() puts back the default
        signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        BUSY["read"], BUSY["write"] = os.pipe()
        pool = parallel_measure._pool(2)
        try:  # both workers in a task, so terminate() can end them only by a SIGTERM
            for _ in range(2):
                pool.apply_async(busy)
            with os.fdopen(BUSY["read"]) as read:
                pids = {int(read.readline()) for _ in range(2)}
            workers = [w for w in pool._pool if w.pid in pids]
            pool.terminate()
        finally:
            os.close(BUSY.pop("write"))
        assert [w.exitcode for w in workers] == [-signal.SIGTERM] * 2
        await asyncio.sleep(0.2)  # a worker's SIGTERM written to the loop's wakeup fd: seen
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
    assert seen == []
    assert signal.SIGTERM not in signal.pthread_sigmask(signal.SIG_BLOCK, [])  # unblocked again


@pytest.mark.timeout(60)
async def test_the_workers_end_after_the_tasks_and_after_a_failed_one():
    """The tasks' results in order, then the pool closed (each worker ends at its queue's end);
    a failed task's error raised, the workers killed (terminate()); with this process's SIGTERM
    handler one that returns, as above."""
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, lambda: None)
    try:
        measure = parallel_measure.Measure(None, 3)
        assert list(measure._run(doubled, [[1], [2, 3], [4]], 3)) == [[2], [4, 6], [8]]
        with pytest.raises(ValueError, match="task"):
            list(measure._run(failed, [[1], [2]], 2))
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


def copied(groups):
    """The private memory (MB) a forked worker gains reading every row of groups[0]."""
    gc.freeze()
    read, write = os.pipe()
    pid = os.fork()
    if pid == 0:  # the worker
        before = parallel_measure.private_mb()
        touched = sum(row["counters"]["completed"] for row in groups[0])
        os.write(write, f"{parallel_measure.private_mb() - before} {touched}".encode())
        os._exit(0)
    os.close(write)
    found = int(os.read(read, 100).split()[0])
    os.waitpid(pid, 0)
    gc.unfreeze()
    return found


@pytest.mark.skipif(not Path("/proc/self/smaps_rollup").exists(), reason="Linux /proc only")
def test_a_worker_copies_the_pages_of_its_groups_only():
    """Objects made group by group (calculator._group_inputs(); the runs and the pulse buckets
    read network by network): a forked worker reading one group of four copies the pages of that
    group; made interleaved (in time order, as before), the pages of all four."""

    def made(order):
        groups = [[] for _ in range(4)]
        for g, n in order:
            groups[g].append({"bucket_start": n, "counters": {"completed": n, "successes": n}})
        return groups

    rows = range(200_000)
    together = copied(made([(n // 50_000, n) for n in rows]))
    interleaved = copied(made([(n % 4, n) for n in rows]))
    assert interleaved > 10 and together < interleaved / 2, (together, interleaved)
