"""procstats: the ps parser, the tree walk, the CPU delta, the summary and the live sampler."""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess

import pytest

from proxy_index.browser import procstats
from proxy_index.browser.procstats import Process, TreeSampler, cpu_seconds, parse, summary

# Host 100 runs a browser (10) with a driver (11) and a renderer (12); 50 is unrelated.
FIRST = """\
  100     1  2000   0:05.00
   10   100  1000   0:02.50
   11    10   500   00:00:01
   12    10   250   1:00.00
   50     1  9999   9:59.99
"""
# One second later: the renderer (12) ended, the browser and driver used 1.5 s more.
SECOND = """\
  100     1  2000   0:05.10
   10   100  1100   0:03.50
   11    10   500   00:00:01.5
   50     1  9999  10:00.99
"""


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("0:00.03", 0.03),
        ("12:34.56", 754.56),
        ("1440:00.00", 86400.0),
        ("00:00:01", 1.0),
        ("01:02:03", 3723.0),
        ("2-03:04:05", 2 * 86400 + 3 * 3600 + 4 * 60 + 5),
    ],
)
def test_cpu_seconds_reads_the_macos_and_linux_time_formats(text, seconds):
    assert cpu_seconds(text) == pytest.approx(seconds)


def test_parse_reads_every_row_and_skips_blank_lines():
    table = parse(FIRST + "\n")
    assert set(table) == {100, 10, 11, 12, 50}
    assert table[10] == Process(ppid=100, rss_bytes=1000 * 1024, cpu_seconds=2.5)
    assert table[12].cpu_seconds == 60.0


@pytest.mark.parametrize("bad", ["10 100 1000\n", "10 100 lots 0:01.00\n", "10 100 1 0:xx\n"])
def test_parse_rejects_a_malformed_row(bad):
    with pytest.raises(ValueError):
        parse(bad)


def test_descendants_walks_the_tree_below_the_root_only():
    table = parse(FIRST)
    assert sorted(procstats.descendants(table, 100)) == [10, 11, 12]
    assert sorted(procstats.descendants(table, 10)) == [11, 12]
    assert procstats.descendants(table, 11) == []


def test_descendants_ignores_a_process_that_is_its_own_parent():
    table = {0: Process(0, 0, 0.0), 1: Process(0, 1024, 0.0)}
    assert procstats.descendants(table, 0) == [1]


def test_record_sums_the_tree_and_counts_an_ended_child_to_its_last_reading():
    sampler = TreeSampler(100)
    first = sampler.record(parse(FIRST), now=10.0)
    assert first["processes"] == 3
    assert first["rss_bytes"] == 1750 * 1024
    assert first["cpu_seconds"] == 63.5
    assert first["cpu_percent"] is None
    second = sampler.record(parse(SECOND), now=11.0)
    # 12 ended at 60 s and still counts: 3.5 + 1.5 + 60 = 65 s, 1.5 s more over 1 s.
    assert second["processes"] == 2
    assert second["rss_bytes"] == 1600 * 1024
    assert second["cpu_seconds"] == 65.0
    assert second["cpu_percent"] == 150.0
    # Nothing ran since: 0 %; over half a second the same 1 s change is 200 %.
    assert sampler.record(parse(SECOND), now=12.0)["cpu_percent"] == 0.0
    later = SECOND.replace("0:03.50", "0:04.50")
    assert sampler.record(parse(later), now=12.5)["cpu_percent"] == 200.0


def test_record_counts_a_reused_pid_as_an_ended_process_and_a_new_one():
    sampler = TreeSampler(100)
    sampler.record(parse(FIRST), now=0.0)
    # pid 12 ended at 60 s and a new renderer took its pid, at 0.5 s so far.
    reused = SECOND + "   12    10   300   0:00.50\n"
    line = sampler.record(parse(reused), now=1.0)
    assert line["cpu_seconds"] == 65.5 and line["cpu_percent"] == 200.0


def test_record_gives_no_percent_without_time_between_readings():
    sampler = TreeSampler(100)
    sampler.record(parse(FIRST), now=5.0)
    assert sampler.record(parse(SECOND), now=5.0)["cpu_percent"] is None


@pytest.mark.parametrize("bad", [0, -1.0])
def test_the_interval_must_be_positive(bad):
    with pytest.raises(ValueError, match="interval"):
        TreeSampler(1, interval=bad)


def test_summary_gives_rss_average_and_peak_and_cpu_average_p95_and_peak():
    lines = [{"rss_bytes": 100, "cpu_percent": None}] + [
        {"rss_bytes": 200 + n, "cpu_percent": float(n)} for n in range(1, 21)
    ]
    assert summary(lines) == {
        "samples": 21,
        "rss_bytes": {"average": 205, "peak": 220},
        "cpu_percent": {"average": 10.5, "p95": 19.0, "peak": 20.0},
    }


def test_summary_of_no_samples_is_empty():
    assert summary([]) == {
        "samples": 0,
        "rss_bytes": {"average": None, "peak": None},
        "cpu_percent": {"average": None, "p95": None, "peak": None},
    }


class FakePopen:
    """subprocess.Popen for `ps`: a fixed listing, exit status, or a hang."""

    listing, returncode, hangs, killed = "", 0, False, False

    def __init__(self, args, **kwargs):
        assert tuple(args) == procstats.PS_COMMAND
        self.pid = 4242

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def communicate(self, timeout=None):
        if FakePopen.hangs:
            raise subprocess.TimeoutExpired("ps", timeout)
        return FakePopen.listing, None

    def kill(self):
        FakePopen.killed = True


@pytest.fixture
def fake_ps(monkeypatch):
    monkeypatch.setattr(FakePopen, "listing", FIRST + "4242  100   800   0:00.01\n")
    monkeypatch.setattr(procstats.subprocess, "Popen", FakePopen)
    return FakePopen


def test_read_ps_drops_the_ps_process_itself(fake_ps):
    table = procstats.read_ps()
    assert table is not None and 4242 not in table and 12 in table


@pytest.mark.parametrize(
    "setup",
    [
        {"returncode": 1},
        {"listing": "not a listing\n"},
        {"hangs": True},
    ],
)
def test_read_ps_is_none_when_ps_cannot_tell(fake_ps, monkeypatch, setup):
    for name, value in setup.items():
        monkeypatch.setattr(FakePopen, name, value)
    monkeypatch.setattr(FakePopen, "killed", False)
    assert procstats.read_ps() is None
    assert FakePopen.killed is bool(setup.get("hangs"))


async def test_stop_writes_the_last_line_and_ends_the_task():
    listings = iter([FIRST, SECOND])
    out = io.StringIO()
    sampler = TreeSampler(100, interval=3600, out=out, read=lambda: parse(next(listings)))
    sampler.start()
    await asyncio.sleep(0)
    await sampler.stop()
    lines = [json.loads(text) for text in out.getvalue().splitlines()]
    assert lines == sampler.lines and len(lines) == 2
    assert [line["processes"] for line in lines] == [3, 2]
    assert lines[-1]["cpu_percent"] is not None
    assert sampler._task is not None and sampler._task.done()
    with pytest.raises(RuntimeError, match="already started"):
        sampler.start()


async def test_a_sample_ps_cannot_give_writes_no_line():
    sampler = TreeSampler(100, interval=3600, read=lambda: None)
    async with sampler:
        await asyncio.sleep(0)
    assert sampler.lines == []


async def test_stop_without_start_does_nothing():
    await TreeSampler(100).stop()


async def test_cancelling_the_run_cancels_the_sampler():
    sampler = TreeSampler(100, interval=0.01, read=lambda: parse(FIRST))

    async def run():
        async with sampler:
            await asyncio.sleep(3600)

    task = asyncio.create_task(run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sampler._task is not None and sampler._task.done()


async def test_a_live_sample_sees_this_process_child():
    child = subprocess.Popen(["sleep", "30"])
    try:
        sampler = TreeSampler(os.getpid(), interval=0.1)
        async with sampler:
            await asyncio.sleep(0.35)
        table = procstats.read_ps()
    finally:
        child.kill()
        child.wait()
    assert table is not None and child.pid in procstats.descendants(table, os.getpid())
    assert len(sampler.lines) >= 3
    assert all(line["processes"] >= 1 and line["rss_bytes"] > 0 for line in sampler.lines)
    assert sampler.lines[0]["cpu_percent"] is None
    assert all(line["cpu_percent"] >= 0 for line in sampler.lines[1:])
    assert summary(sampler.lines)["samples"] == len(sampler.lines)
