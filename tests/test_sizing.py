"""INGEST-SCALE-1: the ingest's and the calculation's limits are settings whose defaults are sized
to the machine (the fixed values used before on a small one), kept within the server's and the
login role's free connections; each stage prints a timing line."""

import logging
import re
import sys

import pytest

from atlas import sizing, timing
from atlas.orchestrator import jobs
from atlas.orchestrator.control import write_settings
from atlas.orchestrator.db import Database
from atlas.storage import connect

pytest_plugins = ["test_storage"]

GIB = sizing.GIB
MACHINE = sizing.machine  # the real one: conftest sizes every test as a small machine


def test_a_small_machine_keeps_the_fixed_limits():
    for cpus in (1, 2, 8, 31):
        assert sizing.derived(sizing.Machine(cpus, 32 * GIB)) == {
            "ingest.processes": max(1, min(7, cpus - 1)),
            "ingest.connections": 25,
            "ingest.memory_budget": 4 * GIB,
            "calculate.parallel_reads": 8,
            "calculate.heavy_parallel": 2,
        }
    assert sizing.most_parallel_reads(sizing.Machine(8, 0)) == 16


def test_the_manager_server_gets_a_quarter_of_its_cpus_and_an_eighth_of_its_memory():
    manager = sizing.Machine(48, 94 * GIB)
    assert sizing.derived(manager) == {
        "ingest.processes": 12,
        "ingest.connections": 36,
        "ingest.memory_budget": 94 * GIB // 8,
        "calculate.parallel_reads": 12,
        "calculate.heavy_parallel": 3,
    }
    assert sizing.most_parallel_reads(manager) == 24


def test_the_machine_is_read(monkeypatch, tmp_path):
    monkeypatch.setattr(sizing.os, "cpu_count", lambda: None)
    assert MACHINE().cpus == 2
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:  4096 kB\nMemAvailable:    2048 kB\n")
    monkeypatch.setattr(sizing, "Path", lambda _: meminfo)
    assert sizing._available() == 2048 * 1024
    meminfo.write_text("MemTotal:  4096 kB\n")  # no MemAvailable: the physical memory
    physical = sizing.os.sysconf("SC_PAGE_SIZE") * sizing.os.sysconf("SC_PHYS_PAGES")
    assert sizing._available() == physical
    meminfo.unlink()

    def unknown(name):
        raise ValueError(name)

    monkeypatch.setattr(sizing.os, "sysconf", unknown)
    assert sizing._available() == 0


def test_the_calculation_scope_is_sized_with_auto(monkeypatch):
    assert sizing.scope_memory("12G") == "12G"
    assert sizing.scope_memory(None) is None
    assert sizing.scope_memory("auto") == "8G"  # a small machine
    monkeypatch.setattr(sizing, "machine", lambda: sizing.Machine(48, 94 * GIB))
    assert sizing.scope_memory("auto") == "11G"
    assert "MemoryMax=11G" in jobs.calculation_argv("db.toml", "m.toml", "11G")


def test_a_setting_overrides_the_machine(database):
    with connect(database) as conn:
        assert sizing.limit(conn, "calculate.parallel_reads") == 8
        write_settings(conn, {"calculate.parallel_reads": 5})
        assert sizing.limit(conn, "calculate.parallel_reads") == 5
        assert sizing.limit(conn, "calculate.work_mem") == "256MB"
        assert sizing.reserves(conn) == (20, 0.25, 10)
        assert len(conn.execute(sizing.CAPACITY).fetchone()) == 4
        assert sizing.capped(conn, 1, "test") == 1


class Capacity:
    """A connection whose server reports (max_connections, in use, role limit, role in use)."""

    def __init__(self, row):
        self.row = row

    def cursor(self, row_factory=None):
        return self

    def execute(self, query):
        return self

    def fetchone(self):
        return self.row


def test_the_role_s_connection_limit_caps_the_connections(monkeypatch, capsys):
    monkeypatch.setattr(sizing, "reserves", lambda conn: (20, 0.25, 10))
    assert sizing.capped(Capacity((100, 30, -1, 5)), 36, "ingest") == 36  # no role limit
    assert sizing.capped(Capacity((100, 30, -1, 5)), 50, "ingest") == 45  # 25 kept free
    err = capsys.readouterr().err
    assert "ingest: warning: 45 connections, not 50: max_connections 100, 30 in use, 25" in err
    assert "role limit" not in err
    assert sizing.capped(Capacity((100, 30, 40, 22)), 36, "calculate") == 8
    assert sizing.capped(Capacity((100, 30, 40, 35)), 36, "calculate") == 1
    err = capsys.readouterr().err
    assert "calculate: warning: 8 connections, not 36: max_connections 100, 30 in use, " in err
    assert "25 kept free; role limit 40, 22 in use, 10 kept free" in err


async def test_the_ingest_sizes_come_from_the_call_the_settings_or_the_machine(database, capsys):
    db = Database(str(database), size=1)
    try:
        sizes = await sizing.ingest_sizes(db, {})
        assert sizes == {"batch": 20, "connections": 25, "processes": 1, "memory_budget": 4 * GIB}
        assert (
            "ingest: batch 20, 25 connections, 1 processes, memory budget 4.0 GiB "
            "(machine: 2 CPUs, 0.0 GiB available)"
        ) in capsys.readouterr().err
        with connect(database) as conn:
            write_settings(conn, {"ingest.connections": 40, "ingest.batch": 10})
        sizes = await sizing.ingest_sizes(db, {"batch": 5})
        assert (sizes["batch"], sizes["connections"], sizes["processes"]) == (5, 40, 1)
        with connect(database) as conn:
            write_settings(conn, {"ingest.processes": 0})
        with pytest.raises(ValueError, match="ingest.processes must be a whole number of at least"):
            await sizing.ingest_sizes(db, {})
    finally:
        await db.close()


def test_a_stage_prints_its_timing_line_also_when_it_fails(capsys):
    with timing.stage("calculate", "read", mode="incremental"):
        pass
    with pytest.raises(KeyError), timing.stage("ingest", "store"):
        raise KeyError("x")
    found = timing.lines("calculate: 3 parallel reads\n" + capsys.readouterr().err)
    assert [re.sub(r" [0-9.]+ s", " T s", row) for row in found] == [
        "timing: calculate read T s mode=incremental",
        "timing: ingest store T s failed=1",
    ]


async def test_the_calculation_s_timing_lines_go_to_the_orchestrator_s_log(caplog):
    code = (
        "import sys; print('timing: calculate read 1.250 s', file=sys.stderr); "
        'print(\'{"snapshot_id": "s1", "as_of": "2026-11-14T10:05:00+00:00"}\')'
    )
    with caplog.at_level(logging.INFO, logger=jobs.log.name):
        result = await jobs.run_calculation([sys.executable, "-c", code], 30)
    assert result["snapshot_id"] == "s1"
    assert [r.getMessage() for r in caplog.records] == ["timing: calculate read 1.250 s"]
