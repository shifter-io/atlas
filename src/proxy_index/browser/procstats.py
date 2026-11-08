"""What the browsers cost a host: CPU % and resident memory of a process tree, per second.

`TreeSampler(root_pid)` is an asyncio task started and stopped around a run (`async with`, or
`start()` and `stop()`). Every `interval` seconds it reads one `ps -A -o pid=,ppid=,rss=,time=`
listing (`ps` only, macOS and Linux), walks the descendants of `root_pid` (the host process:
the browsers and their drivers, never the `ps` itself) and writes one JSON line:

- `at`: the wall-clock time of the reading (ISO 8601, UTC);
- `processes`: how many descendants are alive;
- `rss_bytes`: their summed resident memory;
- `cpu_seconds`: their summed cumulative CPU time (`time=`). A process that ended since the
  last sample keeps counting up to its last reading, so the sum never goes back;
- `cpu_percent`: the change in `cpu_seconds` since the last sample divided by the time
  between the two readings, x 100 (100 = one core busy); null on the first line.

`stop()` takes and writes one last sample, then waits for the task, so the sampler never
outlives its run. A sample `ps` cannot give (missing, failed, unreadable) writes no line.
`summary(lines)` turns the lines into the run's average and peak RSS and its average, p95
and peak CPU %.

Limits: a process born and ended between two samples is not seen. Linux `ps` reports `time=`
in whole seconds (`[DD-]HH:MM:SS`), so there a single sample's `cpu_percent` moves in steps of
100 / interval and only averages over many samples are fine-grained; macOS reports
hundredths (`MM:SS.ss`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TextIO

from ..campaign.sampler import percentile

PS_COMMAND = ("ps", "-A", "-o", "pid=,ppid=,rss=,time=")
PS_TIMEOUT_S = 5


@dataclass(frozen=True, slots=True)
class Process:
    """One `ps` row."""

    ppid: int
    rss_bytes: int
    cpu_seconds: float


def cpu_seconds(text: str) -> float:
    """A `ps` `time=` value in seconds: `MM:SS.ss` (macOS), `[DD-]HH:MM:SS` (Linux)."""
    days, _, clock = text.rpartition("-")
    total = 0.0
    for part in clock.split(":"):
        total = total * 60 + float(part)
    return total + int(days or 0) * 86400


def parse(listing: str) -> dict[int, Process]:
    """Every row of a `pid= ppid= rss= time=` listing (RSS in KiB), by pid; ValueError for a
    malformed row."""
    table = {}
    for line in listing.splitlines():
        if not line.strip():
            continue
        pid, ppid, kib, used = line.split()
        table[int(pid)] = Process(int(ppid), int(kib) * 1024, cpu_seconds(used))
    return table


def read_ps() -> dict[int, Process] | None:
    """One listing of every process except this `ps` itself; None when `ps` cannot tell."""
    try:
        with subprocess.Popen(PS_COMMAND, stdout=subprocess.PIPE, text=True) as ps:
            try:
                listing, _ = ps.communicate(timeout=PS_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                ps.kill()
                raise
        table = parse(listing) if ps.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if table is not None:
        table.pop(ps.pid, None)
    return table


def descendants(table: Mapping[int, Process], root: int) -> list[int]:
    """The pids below `root` (children, their children, ...), `root` itself excluded."""
    children: dict[int, list[int]] = defaultdict(list)
    for pid, process in table.items():
        if pid != process.ppid:
            children[process.ppid].append(pid)
    found, stack = [], list(children[root])
    while stack:
        pid = stack.pop()
        found.append(pid)
        stack.extend(children[pid])
    return found


class TreeSampler:
    """One JSON line per `interval` seconds on `out` (and in `lines`) for `root_pid`'s tree."""

    def __init__(
        self,
        root_pid: int,
        interval: float = 1.0,
        *,
        out: TextIO | None = None,
        read: Callable[[], dict[int, Process] | None] = read_ps,
    ) -> None:
        if not interval > 0:
            raise ValueError("interval: must be positive")
        self.root_pid, self.interval, self.out = root_pid, interval, out
        self.lines: list[dict[str, Any]] = []
        self._read = read
        self._last: dict[int, float] = {}  # each live descendant's CPU seconds, last sample
        self._ended = 0.0  # the last readings of the descendants that ended since
        self._previous: tuple[float, float] | None = None  # (monotonic, cpu_seconds)
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> TreeSampler:
        self.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.stop()

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("TreeSampler: already started")
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Take and write the last sample, then end the task; a no-op if never started."""
        if self._task is None:
            return
        self._stopping.set()
        await self._task

    def record(self, table: Mapping[int, Process], now: float) -> dict[str, Any]:
        """The line for one listing read at monotonic time `now`."""
        tree = {pid: table[pid] for pid in descendants(table, self.root_pid)}
        live = {pid: process.cpu_seconds for pid, process in tree.items()}
        for pid, last in self._last.items():
            if live.get(pid, -1.0) < last:  # ended, or its pid was reused since
                self._ended += last
        self._last = live
        total = self._ended + sum(live.values())
        percent = None
        if self._previous is not None and now > self._previous[0]:
            then, before = self._previous
            percent = round((total - before) / (now - then) * 100, 1)
        self._previous = (now, total)
        return {
            "at": datetime.now(UTC).isoformat(),
            "processes": len(tree),
            "rss_bytes": sum(process.rss_bytes for process in tree.values()),
            "cpu_seconds": round(total, 2),
            "cpu_percent": percent,
        }

    async def _sample(self) -> None:
        table = await asyncio.to_thread(self._read)
        if table is None:
            return
        line = self.record(table, time.monotonic())
        self.lines.append(line)
        if self.out is not None:
            self.out.write(json.dumps(line) + "\n")
            self.out.flush()

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        due = loop.time()
        await self._sample()
        stopped = False
        while not stopped:  # the sample after stop() is always read after it
            due += self.interval
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), max(due - loop.time(), 0))
            stopped = self._stopping.is_set()
            await self._sample()


def summary(lines: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """A run's samples: RSS average and peak, CPU % average, p95 and peak (None when absent)."""
    lines = list(lines)
    rss = [line["rss_bytes"] for line in lines]
    cpu = [line["cpu_percent"] for line in lines if line["cpu_percent"] is not None]
    p95 = percentile(cpu, 0.95)
    return {
        "samples": len(lines),
        "rss_bytes": {
            "average": round(sum(rss) / len(rss)) if rss else None,
            "peak": max(rss, default=None),
        },
        "cpu_percent": {
            "average": round(sum(cpu) / len(cpu), 1) if cpu else None,
            "p95": p95,
            "peak": max(cpu, default=None),
        },
    }
