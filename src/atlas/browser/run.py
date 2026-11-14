"""The browser host's run helpers (browser/node.py): a run's slot, deadline and try timeout,
its timing recorder, transport and sink, the host's size-rotated JSON log and the stall watch."""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from ..models import RunConfig
from ..sinks import JsonSink
from . import procstats, rawfiles
from .settings import Schedule
from .transport import BrowserTransport, Recorder
from .transport import try_seconds as try_seconds

DEADLINE_MARGIN_S = 120
LOG_BYTES = 20 << 20
LOG_BACKUPS = 5


def slot_start(start_at: str, every_minutes: int) -> int:
    """The start (Unix time) of the target slot holding start_at."""
    every = every_minutes * 60
    return math.floor(datetime.fromisoformat(start_at).timestamp() / every) * every


def slot_deadline(start_at: str, every_minutes: int) -> float:
    """The run deadline: the end of the target slot holding start_at, minus 2 minutes."""
    return slot_start(start_at, every_minutes) + every_minutes * 60 - DEADLINE_MARGIN_S


def run_slot(start_at: str, every_minutes: int, schedule: Schedule) -> int:
    """The run's slot (Unix time; its bundle's archive key): in queue mode its own start, which
    a catch-up run takes from a free position (orchestrator/browser.py), else slot_start."""
    if schedule.plan == "queue":
        return math.floor(datetime.fromisoformat(start_at).timestamp())
    return slot_start(start_at, every_minutes)


def run_deadline(start_at: str, every_minutes: int, schedule: Schedule) -> float:
    """The run deadline: in queue mode start_at + run_deadline_minutes minus 2 minutes, so a
    run started at 23:30 has until about 02:28; else slot_deadline."""
    if schedule.plan == "queue":
        start = datetime.fromisoformat(start_at).timestamp()
        return start + schedule.run_deadline_minutes * 60 - DEADLINE_MARGIN_S
    return slot_deadline(start_at, every_minutes)


def _iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, UTC).isoformat()


def _percentile(values: list[int], share: float) -> int | None:
    """Nearest-rank percentile; None for no values."""
    ordered = sorted(values)
    return ordered[max(math.ceil(share * len(ordered)) - 1, 0)] if ordered else None


class RunTiming:
    """One run's recorder: keeps the timing of every try and passes it on to `inner`. Its
    `sampler`, if any, samples the host's process tree while the run's transport is open."""

    def __init__(self, inner: Recorder, sampler: procstats.TreeSampler | None = None) -> None:
        self.inner, self.sampler = inner, sampler
        self.tries = self.ok = self.busy_ms = 0
        self.latency: list[int] = []
        self.queue: list[int] = []

    def try_done(self, record: dict[str, Any], screenshot: bytes | None) -> None:
        timings = record["timings_ms"]
        self.tries += 1
        self.ok += record["error"] is None
        if timings["navigate"] is not None:
            self.latency.append(timings["navigate"] + (timings["settle"] or 0))
        if timings["queue"] is not None:  # it held a browser from the lease to its end
            self.queue.append(timings["queue"])
            self.busy_ms += max((timings["total"] or 0) - timings["queue"], 0)
        self.inner.try_done(record, screenshot)


class RunTransport(BrowserTransport):
    """BrowserTransport that reports when its run ends (the runner leaves the transport), its
    recorder's sampler running from the run's start to its end."""

    def __init__(self, config: RunConfig, *, finished: Callable[[RunConfig], None], **options):
        super().__init__(config, **options)
        self._finished = finished
        self.sampler = getattr(self.recorder, "sampler", None)

    async def __aenter__(self) -> RunTransport:
        if self.sampler is not None:
            self.sampler.start()
        return self

    async def __aexit__(self, *args: object) -> None:
        if self.sampler is not None:
            await self.sampler.stop()
        self._finished(self.config)


class JsonLog:
    """JSON lines, size-rotated: LOG_BYTES per file, LOG_BACKUPS old files kept."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handler = RotatingFileHandler(path, maxBytes=LOG_BYTES, backupCount=LOG_BACKUPS)
        handler.setFormatter(logging.Formatter("%(message)s"))
        self.handler = handler

    def write(self, line: dict[str, Any]) -> None:
        message = json.dumps(line, sort_keys=True)
        self.handler.handle(logging.makeLogRecord({"msg": message, "levelno": logging.INFO}))

    def close(self) -> None:
        self.handler.close()


class BundleSink(JsonSink):
    """The run's JsonSink; its run directory, once made, is the recorder's."""

    def __init__(self, output: Path, recorder: rawfiles.FileRecorder) -> None:
        super().__init__(output)
        self.recorder = recorder

    def start(self, run_id: str) -> None:
        super().start(run_id)
        assert self.directory is not None
        self.recorder.bind(self.directory)


class StallWatch:
    """Each run's counters and when they last moved (IDLE-TIMEOUT-1): a run whose counters have
    stood still for `limit_s` is stalled, and the host stops it and reports it failed."""

    def __init__(self, limit_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit_s, self.clock = limit_s, clock
        self._seen: dict[int, tuple[dict[str, int], float]] = {}
        self._stalled: set[int] = set()

    def seen(self, batch_id: int, counters: Mapping[str, int]) -> bool:
        """Note the run's counters; True once, when it has just become stalled."""
        now, last = self.clock(), self._seen.get(batch_id)
        if last is None or last[0] != counters:
            self._seen[batch_id] = (dict(counters), now)
            return False
        if batch_id in self._stalled or now - last[1] < self.limit_s:
            return False
        self._stalled.add(batch_id)
        return True

    def stalled(self, batch_id: int) -> bool:
        return batch_id in self._stalled

    def forget(self, batch_id: int) -> None:
        self._seen.pop(batch_id, None)
        self._stalled.discard(batch_id)
