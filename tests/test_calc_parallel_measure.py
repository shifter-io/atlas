"""CALC-SCALE-1 (#1125): an incremental build's product groups measured in worker processes
(calculate.workers; calculated/parallel_measure.py) give the build one process gives: the
incremental build with workers equals the full calculation, as the one-process build does
(test_calculated_incremental), read back. Workers only read, each over its own connection in the
build's snapshot; the calculation writes their lists in one process's order.
"""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from calculated_cases import lenient_policy
from test_calculated_hourly_reads import HOUR, floor_hour
from test_calculated_incremental import first_difference, history, without

from atlas.calculated import parallel_measure, store
from atlas.calculated.calculator import calculate
from atlas.storage import connect

pytest_plugins = ["test_storage"]


def equal_to_full(database, policy, as_of, workers):
    """The incremental build with workers, then the full one: equal as read back."""
    new = calculate(database, policy, as_of=as_of, mode="incremental", workers=workers)
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
    for hours in (30, 0):  # the second build reuses some lists and counts the others
        payload = equal_to_full(database, policy, end - hours * HOUR, 3)
    assert "calculate: 3 workers for " in capsys.readouterr().err
    with connect(database) as conn:  # every window names a list the build has
        found = conn.execute(
            "SELECT count(*) FILTER (WHERE l.list_id IS NULL) FROM calculated_windows w "
            "LEFT JOIN calculated_lists l USING(list_id) WHERE w.build_id=%s AND w.target=''",
            (int(payload["snapshot_id"]),),
        ).fetchone()
    assert found == (0,)


def test_the_workers_setting_is_a_whole_number_from_1_to_32():
    assert parallel_measure.workers(1) == 1 and parallel_measure.workers(32) == 32
    for wrong in (0, 33, "4", 2.0, None):
        with pytest.raises(ValueError, match="calculate.workers"):
            parallel_measure.workers(wrong)
    assert parallel_measure.measure(None, "full", 4) is None  # the oracle measures here
    assert parallel_measure.measure(None, "incremental", 1) is None


def test_shares_balance_the_products_bucket_rows():
    rows = {(1, "a"): [0] * 90, (1, "b"): [0] * 9, (2, "a"): [0] * 50, (3, "a"): [0] * 40}
    step = SimpleNamespace(bucket_groups=rows)
    products = [[(1, "a"), (1, "b")], [(2, "a")], [(3, "a")]]
    two = [[(1, "a"), (1, "b")], [(2, "a"), (3, "a")]]
    assert parallel_measure._shares(products, 2, step) == two
    assert parallel_measure._shares(products, 8, step) == [two[0], [(2, "a")], [(3, "a")]]
