"""ORCH-MEM-2: the memory job reads glibc's malloc and gives its free pages back to the system."""

import logging
import threading

import pytest
from orchestrator_cases import query, set_setting

from atlas.orchestrator import malloc, memory
from atlas.orchestrator.memory import MemoryGauge, resident

pytest_plugins = ["test_storage", "admin_cases"]

GLIBC = pytest.mark.skipif(malloc.LIBC is None, reason="glibc's malloc only (CI runs on Linux)")


class FakeLibc:
    def __init__(self, gave_back=1):
        self.trims, self.gave_back = [], gave_back

    def mallinfo2(self):
        return malloc._Mallinfo2(uordblks=30 << 20, hblkhd=10 << 20, fordblks=500 << 20)

    def malloc_trim(self, pad):
        self.trims.append(pad)
        return self.gave_back


def test_stats_are_in_use_and_free_in_mb():
    assert malloc.stats(FakeLibc()) == {"malloc_used_mb": 40, "malloc_free_mb": 500}


def test_trim_trims_every_arena_and_says_whether_it_gave_back():
    libc = FakeLibc()
    assert malloc.trim(libc) is True and libc.trims == [0]
    assert malloc.trim(FakeLibc(gave_back=0)) is False


def test_off_glibc_nothing_is_read_or_trimmed(monkeypatch):
    assert malloc.stats(None) == {} and malloc.trim(None) is False

    class NoMalloc:  # musl, macOS: no mallinfo2
        pass

    monkeypatch.setattr(malloc.ctypes, "CDLL", lambda name: NoMalloc())
    assert malloc.load() is None


@GLIBC
def test_on_glibc_the_process_malloc_is_read():
    found = malloc.stats()
    assert found["malloc_used_mb"] > 0 and found["malloc_free_mb"] >= 0
    assert "malloc_used_mb" in MemoryGauge().measure()


def fragment(megabytes: int) -> list[bytes]:
    """In a thread of its own (its own arena, as an import in the default executor), allocate
    `megabytes` of 1 kB blocks and keep one in 64: the rest is freed but stays with the process,
    held between the kept ones."""
    kept: list[bytes] = []

    def work() -> None:
        blocks = [bytes(1000) for _ in range(megabytes * 1000)]
        kept.extend(blocks[::64])

    thread = threading.Thread(target=work)
    thread.start()
    thread.join()
    return kept


@GLIBC
async def test_the_job_gives_the_memory_a_burst_freed_back_to_the_system(db, caplog):
    kept = fragment(200)
    held = resident()["anon_mb"]
    with caplog.at_level(logging.INFO, logger=memory.__name__):
        found = await MemoryGauge().check(db)
    assert found["trimmed_mb"] >= 100, (held, found)
    assert resident()["anon_mb"] <= held - 100
    assert f"trimmed_mb={found['trimmed_mb']}" in caplog.text
    assert len(kept) == 3125  # still alive: only the free pages went


async def test_with_trimming_off_the_job_only_measures(db, monkeypatch):
    libc = FakeLibc()
    real = malloc.trim
    monkeypatch.setattr(malloc, "trim", lambda: real(libc))
    await set_setting(db, "orchestrator.memory_trim", False)
    found = await MemoryGauge().check(db)
    assert "trimmed_mb" not in found and libc.trims == []


async def test_the_alert_compares_the_memory_left_after_the_trim(database, db, monkeypatch):
    readings = iter([{"rss_mb": 5000, "anon_mb": 4000}, {"rss_mb": 3000, "anon_mb": 2000}])
    monkeypatch.setattr(memory, "resident", lambda: next(readings))
    monkeypatch.setattr(malloc, "trim", lambda: True)
    found = await MemoryGauge().check(db)  # the default limit, 4096 MB
    assert (found["rss_mb"], found["anon_mb"], found["trimmed_mb"]) == (3000, 2000, 2000)
    assert query(database, "SELECT count(*) FROM alerts") == [(0,)]
