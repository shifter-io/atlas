"""One-country calibration using the same census runner and private streaming writer."""

from __future__ import annotations

import asyncio
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import RunConfig
from ..regions import load_regions
from ..runner import run
from .files import atomic_json, lock, private_dir
from .manifest import DEFAULTS, moment, settings, timestamp
from .scheduler import Clock, peak_memory_mb
from .writer import ObservationWriter, SummarySink


@dataclass(slots=True)
class _Probe:
    """What one probe run needs; ``save`` writes ``state`` to ``state.json`` in ``directory``."""

    directory: Path
    state: dict
    clock: Clock
    stop: asyncio.Event
    proxy: Any
    region: str
    country: str
    effective: dict
    run_country: Callable

    def save(self) -> None:
        self.state["updated_at"] = timestamp(self.clock.now())
        self.state["peak_memory_mb"] = peak_memory_mb()
        atomic_json(self.directory / "state.json", self.state)


def _scheduled_start(start: str | None, clock: Clock) -> float:
    scheduled = moment(start).timestamp() if start else clock.now()
    if start and scheduled <= clock.now():
        raise ValueError("Probe start must be in the future")
    if start and scheduled % 3600:
        raise ValueError("Scheduled probe start must be a whole UTC hour")
    return scheduled


def _new_state(
    region: str, country: str, metadata: dict, scheduled: float, effective: dict
) -> dict:
    return {
        "schema_version": 1,
        "kind": "probe",
        "status": "armed",
        "region": region,
        "country": country.lower(),
        "provider": metadata.get("provider"),
        "product": metadata.get("product"),
        "scheduled_at": timestamp(scheduled),
        "settings": effective,
        "progress": {},
        "pid": os.getpid(),
        "active": None,
    }


def _reporter(probe: _Probe, writer: ObservationWriter, began: float, failure: list):
    """The runner's progress callback; a checkpoint error stops the run into ``failure[0]``."""
    previous = [time.monotonic(), 0]

    async def progress(counters):
        now = time.monotonic()
        rate = (counters["requests"] - previous[1]) / max(now - previous[0], 0.001)
        previous[:] = [now, counters["requests"]]
        try:
            writer.check()
            probe.state["progress"] = dict(counters) | {
                "requests_per_second": round(rate, 2),
                "observations_saved": writer.rows,
                "elapsed_seconds": probe.clock.now() - began,
            }
            probe.save()
        except Exception as exc:
            failure[0] = exc
            probe.stop.set()

    return progress


async def _measure(
    probe: _Probe, writer: ObservationWriter, sink: SummarySink, began: float, deadline: float
):
    """Run the country; return ``(summary, error)`` after the writer has been closed."""
    failure = [None]
    summary = None
    error = None
    try:
        summary = await probe.run_country(
            RunConfig(
                "generic_http",
                probe.proxy,
                requests=None,
                stop_rule="unique_ratio",
                runner_location=probe.region,
                **probe.effective,
            ),
            sink,
            stop=probe.stop,
            deadline=deadline,
            aggregate_ips=False,
            wall_clock=probe.clock.now,
            observation_consumer=writer.observe,
            progress=_reporter(probe, writer, began, failure),
            progress_interval=5,
        )
    except BaseException as exc:
        error = exc
    finally:
        try:
            await writer.close()
        except Exception as exc:
            error = error or exc
    return summary, error or failure[0]


async def _run(probe: _Probe, max_seconds: float) -> dict:
    """Measure for at most ``max_seconds`` and save the outcome to ``summary.json``."""
    clock, state = probe.clock, probe.state
    began = clock.now()
    deadline = began + max_seconds
    state.update(
        status="running",
        started_at=timestamp(began),
        ends_at=timestamp(deadline),
        active={"country": probe.country.lower(), "started_at": timestamp(began)},
    )
    probe.save()
    writer = ObservationWriter(
        probe.directory / "observations",
        country=probe.country,
        scheduled=state["scheduled_at"],
        started=timestamp(began),
        deadline=deadline,
    )
    sink = SummarySink()
    summary, error = await _measure(probe, writer, sink, began, deadline)
    summary = summary or getattr(sink, "summary", None)
    state.update(
        status="failed" if error else summary.status if summary else "failed",
        active=None,
        finished_at=timestamp(clock.now()),
        total_duration_seconds=clock.now() - began,
        observations=writer.rows,
        chunks=writer.chunks,
        error_type=type(error).__name__ if error else None,
        measurement=summary.to_dict() if summary else None,
    )
    probe.save()
    atomic_json(probe.directory / "summary.json", state)
    if error:
        raise RuntimeError(
            f"Probe failed ({type(error).__name__}); partial results saved"
        ) from None
    return state


async def execute_probe(
    directory: Path,
    proxy,
    region: str,
    country: str,
    *,
    concurrency: int = 1000,
    processes: int = 0,
    request_timeout: float = 10,
    start: str | None = None,
    max_seconds: float = 86400,
    stop: asyncio.Event | None = None,
    run_country=run,
) -> dict:
    mapping = load_regions()
    country = country.upper()
    if mapping.region_of(country) != region:
        raise ValueError("Country does not belong to the requested runner region")
    effective = settings(
        DEFAULTS
        | {"concurrency": concurrency, "timeout_seconds": request_timeout, "processes": processes}
    )
    if not math.isfinite(max_seconds) or max_seconds <= 0:
        raise ValueError("max_seconds must be finite and positive")
    metadata = proxy.public()
    if str(metadata.get("requested_country", "")).lower() != country.lower():
        raise ValueError("Provider must target the requested country")
    clock = Clock()
    scheduled = _scheduled_start(start, clock)
    stop = stop if stop is not None else asyncio.Event()
    private_dir(directory)
    with lock(directory / ".lock"):
        if (directory / "state.json").exists():
            raise ValueError("Probe directory already exists; choose a new output directory")
        state = _new_state(region, country, metadata, scheduled, effective)
        probe = _Probe(
            directory, state, clock, stop, proxy, region, country, effective, run_country
        )
        probe.save()
        while clock.now() < scheduled and not stop.is_set():
            await asyncio.sleep(min(1, scheduled - clock.now()))
            probe.save()
        if stop.is_set():
            state["status"] = "stopped"
            probe.save()
            return state
        return await _run(probe, max_seconds)
