"""CALC-POINT-IPS-1: the series points' distinct IPs (calculated/point_ips.py) are counted once
per point, hash-aggregated, in batches of at most calculate.point_rows estimated observation
rows, on helper connections in the calculation's snapshot when there is more than one; the
values equal one batch's and a fresh count's, for a first build and an incremental one."""

import json
import re
import subprocess
import sys
from datetime import UTC, datetime

import psycopg
import pytest
from calculated_cases import lenient_policy
from test_calc_hourly import fixture, points_at, reference_points
from test_calc_parallel_reads import opened_helpers, rows
from test_calculated_hourly_reads import HOUR, floor_hour
from test_calculated_incremental import history, without

from proxy_index.calculated import hourly, parallel_reads, point_ips, store
from proxy_index.calculated.calculator import calculate
from proxy_index.orchestrator.control import write_settings
from proxy_index.providers.credentials import read_credentials
from proxy_index.storage import connect

pytest_plugins = ["test_storage"]


def test_cut_bounds_each_batch_and_puts_the_largest_first():
    cut = point_ips.cut
    assert cut({0: 5, 1: 5, 2: 30, 3: 1, 4: 0}, 10) == [(30, [2]), (10, [0, 1]), (1, [3, 4])]
    assert cut({0: 3, 1: 3}, 6) == [(6, [0, 1])]  # at most, not below
    assert cut({0: 0}, 1) == [(0, [0])]


def test_the_module_imports_first():
    """point_ips.py imports parallel_reads.py, whose imports reach hourly_windows.py, which
    imports point_ips.py back: no name of it may be needed while it loads."""
    code = "import proxy_index.calculated.point_ips"
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


def recorded(monkeypatch):
    """The items point_ips() counts and the batches it cuts, from now on."""
    items, batches = [], []
    original_points, original_cut = point_ips.point_ips, point_ips.cut

    def points(conn, wanted):
        items.extend(wanted)
        return original_points(conn, wanted)

    def cut(sizes, most):
        batches.append(original_cut(sizes, most))
        return batches[-1]

    monkeypatch.setattr(point_ips, "point_ips", points)
    monkeypatch.setattr(point_ips, "cut", cut)
    return items, batches


def counted(conn, runs, as_of):
    points = points_at(as_of)
    return hourly.bucket_ips(conn, dict.fromkeys(points, runs), points)


@pytest.mark.asyncio
async def test_batches_on_helpers_equal_one_batch_and_a_fresh_count(
    database, tmp_path, monkeypatch, capsys
):
    end = floor_hour(datetime.now(UTC)) + HOUR
    _, runs = await fixture(database, tmp_path, end)
    opened = opened_helpers(monkeypatch)
    items, batches = recorded(monkeypatch)
    with connect(database) as conn:
        one = counted(conn, runs, end)  # the defaults: one light batch on conn, no helper
        assert len(batches) == 1 and len(batches[0]) == 1 and opened == []
        # Each point counted once: the 7- and 30-day series share their daily points.
        assert len({tuple(item.clips) for item in items}) == len(items)
        shared = [at for at in one[168] if at in one[720]]
        assert len(shared) >= 2 and all(one[168][at] == one[720][at] for at in shared)
        for hours, (start, until, step) in points_at(end).items():
            assert one[hours] == reference_points(conn, runs, start, until, step), hours
        conn.rollback()  # the memos too: the points are counted again
        write_settings(conn, {"calculate.point_rows": 1})
        assert counted(conn, runs, end) == one
        assert len(batches[-1]) == len(items) // 2  # a batch per point
        sizes = sorted(rows for rows, _ in batches[-1])
        conn.rollback()
        # Heavy and light batches: the heavy ones on their own lane at heavy_work_mem.
        middle = sizes[len(sizes) // 2]
        write_settings(conn, {"calculate.point_rows": 1, "calculate.heavy_rows": middle})
        capsys.readouterr()
        assert counted(conn, runs, end) == one
        conn.rollback()
    err = capsys.readouterr().err
    found = re.search(r"(\d+) heavy at 1GB \(of 2\) for (\d+) point batches above", err)
    assert found and int(found[1]) >= 1 and int(found[2]) >= 1, err
    assert re.search(r", [1-9]\d* light at 256MB for [1-9]\d* point batches;", err), err
    assert re.search(r"series point IPs: \d+ points in \d+ batches of at most 1 rows", err), err
    assert re.search(r"temp written \d+ MB", err), err
    assert opened and all(c.closed for c in opened)


@pytest.mark.asyncio
async def test_point_ips_are_hash_aggregated_without_a_sort(database, tmp_path, monkeypatch):
    end = floor_hour(datetime.now(UTC)) + HOUR
    _, runs = await fixture(database, tmp_path, end)
    columns, original = [], point_ips._count

    def count(conn, wanted):
        columns.append(wanted)
        return original(conn, wanted)

    monkeypatch.setattr(point_ips, "_count", count)
    with connect(database) as conn:
        counted(conn, runs, end)
        query = "EXPLAIN (FORMAT JSON) " + point_ips.BUCKET_IPS
        plan = point_ips.hashed(conn, query, columns[0])[0]["QUERY PLAN"][0]["Plan"]
        assert conn.execute("SHOW enable_sort").fetchone() == ("on",)  # for this statement only
        conn.rollback()
    nodes, todo = [], [plan]
    while todo:
        node = todo.pop()
        nodes.append((node["Node Type"], node.get("Strategy")))
        todo += node.get("Plans", [])
    # The DISTINCT and the count per point: two HashAggregates, no sort.
    assert nodes[:2] == [("Aggregate", "Hashed")] * 2, nodes
    assert all(kind not in ("Sort", "Incremental Sort", "Unique") for kind, _ in nodes), nodes


@pytest.mark.asyncio
async def test_a_failing_batch_fails_and_closes_the_helpers(database, tmp_path, monkeypatch):
    end = floor_hour(datetime.now(UTC)) + HOUR
    _, runs = await fixture(database, tmp_path, end)
    opened, calls, original = opened_helpers(monkeypatch), [], point_ips._count

    def failing(conn, columns):
        calls.append(columns)
        if len(calls) == 2:
            conn.execute("SELECT 1/0")
        return original(conn, columns)

    monkeypatch.setattr(point_ips, "_count", failing)
    with connect(database) as conn:
        write_settings(conn, {"calculate.point_rows": 1})
        with pytest.raises(psycopg.errors.DivisionByZero):
            counted(conn, runs, end)
        conn.rollback()
    assert opened and all(c.closed for c in opened)


@pytest.mark.asyncio
async def test_a_calculation_in_batches_equals_one_in_one_batch(
    database, databases, tmp_path, monkeypatch, capsys
):
    """A first build, then one an hour later, with the default point_rows and with 1 on two
    copies of one database: the same payloads and calculated_* rows (calculated_series too)."""
    end = floor_hour(datetime.now(UTC)) + HOUR
    await history(database, tmp_path, end)
    other = databases(template=read_credentials(database, "postgres")["dbname"])
    with connect(other) as conn:
        write_settings(conn, {"calculate.point_rows": 1})
    policy = lenient_policy(tmp_path)
    for as_of in (end - HOUR, end):
        one = calculate(database, policy, as_of=as_of, mode="incremental")
        capsys.readouterr()
        batched = calculate(other, policy, as_of=as_of, mode="incremental")
        err = capsys.readouterr().err
        if as_of < end:  # the first build counts every point, on helpers
            assert re.search(r"series point IPs: \d+ points in \d+ batches", err), err
        a = without(store.read(database, one["snapshot_id"]))
        b = without(store.read(other, batched["snapshot_id"]))
        assert json.dumps(a) == json.dumps(b), as_of
        a, b = rows(database), rows(other)
        assert a["calculated_series"] and [t for t in a if a[t] != b[t]] == [], as_of


@pytest.mark.asyncio
async def test_parallel_reads_bounds_the_point_helpers_too(database, tmp_path, monkeypatch, capsys):
    """`calculate --parallel-reads 1` (kept in the calculation's transaction by configure())
    gives the point step one helper, not the settings table's 8."""
    end = floor_hour(datetime.now(UTC)) + HOUR
    await history(database, tmp_path, end)
    with connect(database) as conn:
        write_settings(conn, {"calculate.point_rows": 1})
    with connect(database) as conn:
        with parallel_reads.Prefetcher(conn) as prefetch:
            prefetch.configure(3)
            assert parallel_reads.settings(conn)["parallel_reads"] == 3
        conn.rollback()  # the override ends with the transaction
        assert parallel_reads.settings(conn)["parallel_reads"] == 8
        conn.rollback()
    opened_helpers(monkeypatch)
    calculate(database, lenient_policy(tmp_path), as_of=end, mode="incremental", parallel_reads=1)
    err = capsys.readouterr().err
    found = re.findall(r"calculate: (\d+) parallel reads \(of (\d+)\)[^\n]* point batches", err)
    assert found == [("1", "1")], err
