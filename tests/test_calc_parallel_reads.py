"""CALC-PARALLEL-1: the incremental calculation reads its missed groups' window inputs over K
helper connections that import its snapshot (calculated/parallel_reads.py), ahead of the group
loop, with a larger work_mem; the payloads and every calculated_* row equal the serial path's,
and a failing helper fails the whole calculation."""

import json
import re
import threading
from collections import defaultdict
from datetime import UTC, datetime

import psycopg
import pytest
from calculated_cases import lenient_policy
from test_calculated_hourly_reads import HOUR, daily_history, floor_hour
from test_calculated_incremental import history, without

from atlas import sizing, timing
from atlas.calculated import incremental, parallel_reads, store
from atlas.calculated.calculator import calculate
from atlas.cli import main
from atlas.orchestrator.control import write_settings
from atlas.providers.credentials import read_credentials
from atlas.storage import connect

pytest_plugins = ["test_storage"]

TABLES = (
    "SELECT table_name FROM information_schema.tables WHERE table_schema='public' "
    "AND table_name LIKE 'calculated%%' AND table_name NOT LIKE 'calculated_snapshot%%' "
    "ORDER BY 1"
)
# now(), and what hashes it, differs between the databases: the slot memos' digests hash their
# run ranges' created_at (hourly.py); the snapshots (store.read()) hold generated_at.
CLOCK = ("created_at", "last_used", "updated_at", "digest")


def rows(database):
    """Every calculated_* row (but the snapshots, read with store.read()), without the clock's
    columns, sorted."""
    found = {}
    with connect(database) as conn:
        for (table,) in conn.execute(TABLES).fetchall():
            cursor = conn.execute(f"SELECT * FROM {table}")
            names = [c.name for c in cursor.description]
            kept = [[v for n, v in zip(names, r, strict=True) if n not in CLOCK] for r in cursor]
            found[table] = sorted(json.dumps(r, default=str) for r in kept)
    return found


def opened_helpers(monkeypatch):
    """The helper connections the prefetcher opens, from now on; the server's free connections
    do not lower K."""
    opened, original = [], parallel_reads.snapshot_connection
    monkeypatch.setattr(sizing, "capped", lambda conn, wanted, name: wanted)

    def counted(conn, exported):
        opened.append(original(conn, exported))
        return opened[-1]

    monkeypatch.setattr(parallel_reads, "snapshot_connection", counted)
    return opened


@pytest.mark.asyncio
async def test_parallel_reads_equal_the_serial_reads(
    database, databases, tmp_path, monkeypatch, capsys
):
    """A first build, then one an hour later, with K = 1 and with K = 8 (`--parallel-reads`)
    on two copies of one database: the same payloads and calculated_* rows."""
    end = floor_hour(datetime.now(UTC)) + HOUR
    await history(database, tmp_path, end)
    other = databases(template=read_credentials(database, "postgres")["dbname"])
    policy = lenient_policy(tmp_path)
    opened, helpers = opened_helpers(monkeypatch), []
    for as_of in (end - HOUR, end):
        before = len(opened)
        serial = calculate(database, policy, as_of=as_of, mode="incremental", parallel_reads=1)
        assert len(opened) == before + 1
        args = ["calculate", "--database-file", str(other), "--methodology", str(policy)]
        capsys.readouterr()
        assert main([*args, "--as-of", as_of.isoformat(), "--parallel-reads", "8"]) == 0
        out = capsys.readouterr()
        # As many helpers as missed groups, up to 8; none is heavy.
        k = int(re.search(r"(\d+) parallel reads \(of 8\): 0 heavy", out.err)[1])
        helpers.append(k)
        assert re.search(r"calculate: temp written \d+ MB", out.err)
        stages = [row.split()[2] for row in timing.lines(out.err)]  # each stage's timing line
        assert stages == ["rollups", "read", "ip_state", "build", "store", "prune"]
        assert len(opened) == before + 1 + k and all(c.closed for c in opened)
        parallel = json.loads(out.out)
        a = without(store.read(database, serial["snapshot_id"]))
        b = without(store.read(other, parallel["snapshot_id"]))
        assert json.dumps(a) == json.dumps(b), as_of
        a, b = rows(database), rows(other)
        assert [t for t in a if a[t] != b[t]] == [], as_of
    assert helpers[0] > 2, helpers  # the first build misses every group


def test_helpers_read_ahead_in_the_snapshot_and_out_of_order_reads_directly(database):
    with connect(database) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        conn.execute("SET LOCAL TimeZone TO 'Asia/Tokyo'")
        conn.execute("SELECT count(*) FROM settings")  # the calculation's snapshot
        with connect(database) as later:  # committed after it: no helper sees it
            write_settings(later, {"calculate.work_mem": "128MB"})
        with parallel_reads.Prefetcher(conn) as prefetch:
            prefetch.configure(2)
            assert prefetch.settings["parallel_reads"] == 2
            assert prefetch.settings["work_mem"] == "256MB"
            query = "SELECT current_setting('work_mem'),current_setting('TimeZone'),count(*) "
            query += "FROM settings"

            def read(c, key):
                return key, c is conn, threading.current_thread().name, *c.execute(query).fetchone()

            prefetch.start(range(10), read, defaultdict(int))  # every key light
            assert len(prefetch.connections) == 2 and list(prefetch.futures)[:2] == [0, 1]
            first, own = prefetch.take(0), read(conn, 0)
            assert first[:2] == (0, False) and first[2].startswith("calc-read")
            assert first[3:] == own[3:] and first[3:5] == ("256MB", "Asia/Tokyo")
            with connect(database) as fresh:
                assert fresh.execute("SELECT count(*) FROM settings").fetchone() == (own[5] + 1,)
            assert prefetch.take(3)[1] is False  # read ahead, or first when wanted
            assert all(k > 3 for k in prefetch.futures)  # 1 and 2 are dropped
            assert prefetch.take(1)[:2] == (1, True)  # dropped: read directly
            assert prefetch.take(42)[:2] == (42, True)  # not in the order
            assert prefetch.take(5)[1] is False
            assert prefetch.take(5)[:2] == (5, True)  # taken already
        assert all(c.closed for c in prefetch.connections) and not prefetch.lanes
        conn.rollback()


def test_heavy_groups_are_read_largest_first_on_their_own_helpers(database, monkeypatch, capsys):
    monkeypatch.setattr(sizing, "capped", lambda conn, wanted, name: wanted)
    rows = defaultdict(int, {2: 5_000_000, 7: 9_000_000, 4: 2_000_000})  # heavy: above 1M
    heavy, done = [], threading.Event()

    def read(c, key):
        heavy.extend([key] if key in (2, 4, 7) else [])  # in the order they start
        found = key, c.execute("SHOW work_mem").fetchone()[0]
        if len(heavy) == 3:
            done.set()
        return found

    with connect(database) as conn:
        write_settings(conn, {"calculate.heavy_parallel": 1})
        with parallel_reads.Prefetcher(conn) as prefetch:
            prefetch.configure(3)
            prefetch.start(range(10), read, rows)
            assert done.wait(30)
            found = [prefetch.take(key) for key in range(10)]
        assert heavy == [7, 2, 4]  # largest first (9M, 5M, 2M rows), one at a time
        assert found == [(k, "1GB" if k in (2, 4, 7) else "256MB") for k in range(10)]
        assert all(c.closed for c in prefetch.connections)
        conn.rollback()
    err = capsys.readouterr().err
    assert (
        "calculate: 3 parallel reads (of 3): 1 heavy at 1GB (of 1) for 3 groups above 1000000 "
        "rows, 2 light at 256MB for 7 groups; worst-case server memory 3072 MB "
        "(max_parallel_memory 6GB)"
    ) in err


def test_plan_splits_and_lowers_the_helpers():
    found = {
        "parallel_reads": 8,
        "heavy_parallel": 2,
        "heavy_rows": 1_000_000,
        "work_mem": "256MB",
        "heavy_work_mem": "1GB",
        "max_parallel_memory": "6GB",
    }
    plan = parallel_reads.plan
    # 2 x 1GB x 2 + 6 x 256MB x 2 = 7GB would pass 6GB: two light helpers fewer.
    assert plan(found, 5, 100) == (2, 4)
    assert plan(found, 0, 100) == (0, 8)  # no heavy group: every helper light
    assert plan(found, 1, 3) == (1, 3)  # no more helpers than groups
    assert plan(found, 5, 0) == (2, 0)
    assert plan(found | {"parallel_reads": 1}, 5, 100) == (1, 0)  # it reads the light ones too
    assert plan(found | {"max_parallel_memory": "1GB"}, 5, 100) == (1, 0)  # never below one
    assert parallel_reads.worst_case(found, 2, 4) == 6 * 1024**3


@pytest.mark.asyncio
async def test_a_failing_helper_fails_the_whole_calculation(database, tmp_path, monkeypatch):
    end = floor_hour(datetime.now(UTC)) + HOUR
    await daily_history(database, tmp_path, end)
    opened, calls, lock = opened_helpers(monkeypatch), [], threading.Lock()
    original = incremental.Incremental._window_inputs

    def failing(conn, specs):
        with lock:
            calls.append(threading.current_thread().name)
            if len(calls) == 2:  # the second group read: its helper's statement fails
                conn.execute("SELECT 1/0")
        return original(conn, specs)

    monkeypatch.setattr(incremental.Incremental, "_window_inputs", staticmethod(failing))
    policy = lenient_policy(tmp_path)
    # The failing read, or one after it on its helper (its transaction is aborted).
    failed = (psycopg.errors.DivisionByZero, psycopg.errors.InFailedSqlTransaction)
    with pytest.raises(failed):
        calculate(database, policy, as_of=end, mode="incremental", publish=True)
    assert len(calls) >= 2 and all(name.startswith("calc-read") for name in calls)
    assert opened and all(c.closed for c in opened)
    with connect(database) as conn:  # nothing written, nothing published
        for table in ("calculation_builds", "current_calculation", "calculated_lists"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,), table


def test_settings_and_their_bounds(database, capsys, monkeypatch):
    monkeypatch.setattr(sizing, "capped", lambda conn, wanted, name: wanted)
    with connect(database) as conn:
        assert parallel_reads.settings(conn) == {
            "parallel_reads": 8,
            "heavy_parallel": 2,
            "heavy_rows": 1_000_000,
            "point_rows": 20_000_000,
            "work_mem": "256MB",
            "heavy_work_mem": "1GB",
            "max_parallel_memory": "6GB",
        }
        write_settings(conn, {"calculate.parallel_reads": 16, "calculate.work_mem": "512MB"})
        assert parallel_reads.settings(conn)["parallel_reads"] == 16
        assert parallel_reads.settings(conn, 3)["parallel_reads"] == 3  # --parallel-reads
        for wrong in (0, 17, "8", True):
            with pytest.raises(ValueError, match="parallel_reads must be a whole number from 1 to"):
                parallel_reads.settings(conn, wrong)
        write_settings(conn, {"calculate.heavy_rows": 0})
        with pytest.raises(ValueError, match="heavy_rows must be a whole number from 1 to any"):
            parallel_reads.settings(conn)
        write_settings(conn, {"calculate.heavy_rows": 10**12, "calculate.heavy_work_mem": "1 GB"})
        with pytest.raises(ValueError, match="Invalid memory setting '1 GB'"):
            parallel_reads.settings(conn)
        for wrong in ("256 MB", "1PB", "32kB", 256, "MB", "١MB"):
            with pytest.raises(ValueError, match="Invalid memory setting"):
                parallel_reads.memory_bytes(wrong)
        assert parallel_reads.memory_bytes("64kB") == 2**16
        write_settings(conn, {"calculate.heavy_work_mem": "1GB"})
        # 16 x 512MB x 2 would pass the 6GB maximum: lowered to 6 light helpers.
        with parallel_reads.Prefetcher(conn) as prefetch:
            prefetch.configure()
            assert conn.execute("SHOW work_mem").fetchone() == ("512MB",)
            prefetch.start([], None, {})  # nothing to read: no helper
            assert not prefetch.lanes and capsys.readouterr().err == ""
            prefetch.start(range(20), lambda c, key: key, defaultdict(int))
            assert len(prefetch.connections) == 6 and prefetch.take(0) == 0
        err = capsys.readouterr().err
        assert "6 parallel reads (of 16): 0 heavy at 1GB (of 2) for 0 groups" in err
        assert "6 light at 512MB for 20 groups; worst-case server memory 6144 MB" in err
        conn.rollback()
