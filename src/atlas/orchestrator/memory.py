"""ORCH-MEM-1: the orchestrator's memory gauge.

On 26-27 Nov 2026 the orchestrator's resident memory grew about 150 MB an hour and nothing
showed what grew. The job `memory` (every 5 minutes) now measures the process: its resident
memory (Linux /proc/self/status: anonymous and file-backed apart, and the peak), its threads,
the blocks Python has allocated, and the size of each structure the process keeps between
requests (MemoryGauge.sizes). It logs them on one line, returns them (the job's last result
in the admin's jobs list) and raises the major alert `orchestrator_memory` while the resident
memory is at or above `orchestrator.memory_alert_mb`; the alert is acknowledged at the first
measure below it. /admin/api/health shows the same numbers.
"""

from __future__ import annotations

import logging
import resource
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aiohttp import web

from .db import Database, setting
from .events import raise_alert

log = logging.getLogger(__name__)

KIND = "orchestrator_memory"
STATUS = Path("/proc/self/status")
FIELDS = {"VmRSS": "rss_mb", "RssAnon": "anon_mb", "RssFile": "file_mb", "VmHWM": "peak_mb"}


def resident(status: Path = STATUS) -> dict[str, int]:
    """The process's resident memory in MB: total, anonymous, file-backed and peak; off Linux
    only the peak (getrusage)."""
    try:
        text = status.read_text()
    except OSError:
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return {"peak_mb": peak // (1 << 20 if sys.platform == "darwin" else 1024)}
    found = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        if name in FIELDS and rest.split():
            found[FIELDS[name]] = int(rest.split()[0]) // 1024  # kB
    return found


Sizes = Callable[[], dict[str, int]]


class MemoryGauge:
    """The process's memory and the sizes its `sources` report: each a callable that returns
    {structure name: its current length} for the structures one owner keeps."""

    def __init__(self, sources: list[Sizes] | None = None) -> None:
        self.sources = list(sources or [])

    def measure(self) -> dict[str, Any]:
        structures: dict[str, Any] = {}
        for source in self.sources:
            try:
                structures |= source()
            except Exception as exc:  # boundary: a gauge must never fail its job
                name = getattr(source, "__qualname__", "source")
                structures[name] = f"unreadable ({type(exc).__name__})"
        return resident() | {
            "threads": threading.active_count(),
            "python_blocks": sys.getallocatedblocks(),
            "structures": dict(sorted(structures.items())),
        }

    async def check(self, db: Database) -> dict[str, Any]:
        """The job `memory`: log the measure, alert at or above the limit, acknowledge below."""
        found = self.measure()
        log.info("memory %s", " ".join(f"{k}={v}" for k, v in _flat(found)))
        rss = found.get("rss_mb")
        async with db.transaction() as conn:
            limit = await setting(conn, "orchestrator.memory_alert_mb")
            if rss is None or limit is None:
                return found
            if rss >= int(limit):
                message = f"the orchestrator holds {rss} MB resident (alert at {int(limit)} MB)"
                data = {"limit_mb": int(limit)} | found
                await raise_alert(conn, KIND, message, severity="major", data=data)
            else:
                await conn.execute(
                    "UPDATE alerts SET acknowledged_at=now() WHERE dedupe_key=%s "
                    "AND acknowledged_at IS NULL",
                    (KIND,),
                )
        return found


GAUGE_KEY: web.AppKey[MemoryGauge] = web.AppKey("memory_gauge", MemoryGauge)


def _flat(found: dict[str, Any]) -> list[tuple[str, Any]]:
    pairs = [(k, v) for k, v in found.items() if k != "structures"]
    return pairs + list(found.get("structures", {}).items())
