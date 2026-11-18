"""A durable hourly FIFO and a single country executor per region."""

from __future__ import annotations

import asyncio
import functools
import os
import resource
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import RunConfig
from ..providers.grammar import answer_classes
from ..runner import RunOptions, run
from . import country_pace
from .files import atomic_json, lock
from .manifest import load, moment, regional_window, settings, timestamp
from .scheduler_state import (  # noqa: F401 (callers keep importing these from here)
    TERMINAL,
    job_directory,
    new_state,
    read_checkpoint,
    recover_running,
    region_settings,
    status,
    validate_state,
)
from .writer import ObservationWriter, SummarySink

# A country test's own limit (DAILY-OVERTIME-1, #1152): it runs until it is finished, but never
# longer than this (RunConfig.cap_seconds; the stop rule ends it as 'cap', its data kept).
COUNTRY_CAP_SECONDS = 3600.0


class Clock:
    def __init__(self):
        self.wall, self.mono = time.time(), time.monotonic()

    def now(self) -> float:
        # Backward wall-clock changes never lengthen a campaign or delay its queue.
        return max(time.time(), self.wall + time.monotonic() - self.mono)

    async def sleep(self, seconds: float):
        await asyncio.sleep(seconds)


def peak_memory_mb() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(value / (1024 * 1024 if sys.platform == "darwin" else 1024), 2)


def save_checkpoint(path: Path, state: dict, clock) -> None:
    state["pid"] = os.getpid()
    state["updated_at"] = timestamp(clock.now())
    state["peak_memory_mb"] = peak_memory_mb()
    # Rewritten after every country, so it skips the slow indented encoder.
    atomic_json(path, state, indent=None)


@dataclass(slots=True)
class JobContext:
    """What one country run needs from the executor that owns its checkpoint document.

    ``state`` is the checkpoint holding ``active``, ``progress`` and ``status``; ``save``
    persists it. ``end`` bounds observations and the stop watcher (``math.inf`` for an
    ephemeral round); ``deadline`` is the runner's deadline (``None`` for no deadline).
    """

    manifest: dict
    region: str
    directory: Path
    effective: dict
    state: dict
    save: Callable[[], None]
    proxy_factory: Callable
    run_country: Callable
    stop: asyncio.Event
    clock: Any
    end: float
    deadline: float | None


def _start_job(context: JobContext, job: dict) -> float:
    state, began = context.state, context.clock.now()
    job.update(
        status="running",
        started_at=timestamp(began),
        delay_seconds=max(0.0, began - moment(job["scheduled_at"]).timestamp()),
    )
    state["active"] = {
        "round": job["round"],
        "country": job["country"],
        "started_at": job["started_at"],
    }
    state["status"] = "running"
    state["progress"] = {}
    context.save()
    return began


def _census_config(context: JobContext, proxy, country: str) -> RunConfig:
    """One country's census run config; ``answer_classes`` come from the network's grammar. A
    country test stops at COUNTRY_CAP_SECONDS however many new IPs it still finds, a daily test's
    at its pace's time budget when it has one; a daily
    test's country runs at its manifest pace (onboarding.country_pace) under the manifest's
    concurrency, which stays its scope's. A low-availability country (its pace's low cap) runs
    beside the cell's other low ones (onboarding.rounds): in one process, growing to at most its
    share of the concurrency."""
    pace = country_pace.of(context.manifest, country)
    effective = dict(context.effective)
    if country_pace.low(context.manifest, country):
        width = country_pace.low_width(context.manifest, effective["concurrency"])
        effective |= {"processes": 1, "grow_ceiling": effective["concurrency"] // width}
    classes = (
        answer_classes(context.manifest["provider"])
        if effective.pop("answer_classes", False)
        else None
    )
    return RunConfig(
        "generic_http",
        proxy,
        requests=pace.get("requests"),
        stop_rule="unique_ratio",
        runner_location=context.region,
        target_url=context.manifest["target_url"],
        answer_classes=classes,
        cap_seconds=min(float(pace.get("seconds", COUNTRY_CAP_SECONDS)), COUNTRY_CAP_SECONDS),
        start_concurrency=pace.get("concurrency"),
        low_requests=pace.get("low_requests"),
        low_seconds=pace.get("low_seconds"),
        **effective,
    )


def _checked_proxy(context: JobContext, job: dict):
    """The country's proxy, refused unless it targets and labels the configured country."""
    manifest = context.manifest
    proxy = context.proxy_factory(job["country"])
    metadata = proxy.public()
    if (
        metadata.get("provider") != manifest["provider"]
        or metadata.get("product") != manifest["product"]
        or str(metadata.get("requested_country", "")).lower() != job["country"].lower()
    ):
        raise ValueError("Provider must target and label the configured country")
    return proxy


async def _measure(context: JobContext, job: dict, writer: ObservationWriter, sink: SummarySink):
    """Run one country; return ``(summary, error)`` after the writer has been closed."""
    clock, stop, end = context.clock, context.stop, context.end
    country_stop = asyncio.Event()
    checkpoint_error = [None]

    async def watch():
        while not stop.is_set() and clock.now() < end:
            await clock.sleep(min(0.05, end - clock.now()))
        country_stop.set()

    async def progress(counters):
        try:
            writer.check()
            context.state["progress"] = dict(counters) | {"observations_saved": writer.rows}
            context.save()
        except (OSError, TypeError, ValueError) as exc:  # the writer, or state.json not saved
            checkpoint_error[0] = exc
            country_stop.set()

    watcher = asyncio.create_task(watch())
    summary = None
    error = None
    try:
        config = _census_config(context, _checked_proxy(context, job), job["country"])
        options = RunOptions(
            stop=country_stop,
            deadline=context.deadline,
            aggregate_ips=False,
            wall_clock=clock.now,
            observation_consumer=writer.observe,
            progress=progress,
            progress_interval=5,
            slices=True,
        )
        summary = await context.run_country(config, sink, options)
    except BaseException as exc:  # boundary: saved in the job's summary.json; run_job re-raises
        error = exc  # an interrupt or a cancellation too, once they are saved
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        try:
            await writer.close()
        except OSError as exc:  # the writer failed (``check``)
            error = error or exc
    return summary, error or checkpoint_error[0]


def _job_status(error, summary, finished: float, end: float) -> str:
    if error is not None:
        return "interrupted" if isinstance(error, asyncio.CancelledError) else "failed"
    if summary is not None and summary.status == "completed":
        # Successful measurement stays complete if local file finalization
        # crosses the campaign deadline.
        return "completed"
    if finished >= end or (summary and summary.stop_reason == "deadline"):
        return "deadline"
    return "interrupted"


async def run_job(context: JobContext, job: dict) -> None:
    """Measure one country of ``job``, write its ``summary.json`` and update ``job``.

    Any measurement or file error is recorded as a ``failed`` (or ``interrupted``) job and
    then re-raised, exactly as the regional executor has always done.
    """
    began = _start_job(context, job)
    folder = job_directory(context.directory, job)
    writer = ObservationWriter(
        folder,
        country=job["country"],
        scheduled=job["scheduled_at"],
        started=job["started_at"],
        deadline=context.end,
    )
    sink = SummarySink()
    summary, error = await _measure(context, job, writer, sink)
    finished = context.clock.now()
    measurements = summary or getattr(sink, "summary", None)
    job.update(
        status=_job_status(error, summary, finished, context.end),
        finished_at=timestamp(finished),
        observations=writer.rows,
        duration_seconds=max(0, finished - began),
        requests=measurements.completed if measurements else 0,
        errors=measurements.total_errors if measurements else 0,
        stop_reason=measurements.stop_reason if measurements else "error",
    )
    atomic_json(
        folder / "summary.json",
        {
            "schema_version": 1,
            "campaign_id": context.manifest["campaign_id"],
            "region": context.region,
            **job,
            "settings": context.effective,
            "chunks": writer.chunks,
            "peak_queue": writer.peak_queue,
            "peak_memory_mb": peak_memory_mb(),
            "measurement": measurements.to_dict() if measurements else None,
        },
    )
    context.state["active"] = None
    context.save()
    if error is not None:
        raise error


def _open_region(
    state_path: Path, manifest: dict, region: str, options: RegionOptions, effective: dict
) -> tuple[dict, dict]:
    """The region's ``(state, settings)``: its checkpoint on resume, else a new state."""
    state = read_checkpoint(state_path, manifest, region, options.resume)
    overrides = (
        ("concurrency", options.concurrency),
        ("timeout_seconds", options.request_timeout),
        ("processes", options.processes),
    )
    effective = region_settings(effective, state, overrides)
    if state is None:
        state = new_state(manifest, region, effective)
    return state, effective


async def _run_queue(context: JobContext) -> None:
    """Run each unfinished job at its hour until the region's end or a stop."""
    state, clock, stop, end = context.state, context.clock, context.stop, context.end
    for job in state["jobs"]:
        if job["status"] in TERMINAL:
            continue
        scheduled = moment(job["scheduled_at"]).timestamp()
        while clock.now() < scheduled and clock.now() < end and not stop.is_set():
            await clock.sleep(min(0.25, scheduled - clock.now(), end - clock.now()))
            if clock.now() - moment(state["updated_at"]).timestamp() >= 5:
                context.save()
        if stop.is_set() or clock.now() >= end:
            break
        await run_job(context, job)


async def _close_queue(context: JobContext) -> None:
    """Wait for the region's end once every job is done, then set the final status."""
    state, clock, stop, end = context.state, context.clock, context.stop, context.end
    if not stop.is_set() and all(j["status"] in TERMINAL for j in state["jobs"]):
        state["status"] = "waiting_for_deadline"
        context.save()
        while clock.now() < end and not stop.is_set():
            await clock.sleep(min(0.25, end - clock.now()))
            if clock.now() - moment(state["updated_at"]).timestamp() >= 5:
                context.save()
    expired = clock.now() >= end
    if expired:
        for job in state["jobs"]:
            if job["status"] == "pending":
                job["status"] = "not_started_deadline"
    state["status"] = "closed" if expired else "stopped" if stop.is_set() else "completed"


async def _drive(context: JobContext) -> dict:
    """Run the queue to its end; save the region's ``summary.json`` and return its status."""
    try:
        await _run_queue(context)
        await _close_queue(context)
    except BaseException:
        context.state["status"] = "failed"
        context.save()
        raise
    context.state["active"] = None
    context.save()
    result = status(context.state, context.clock.now())
    atomic_json(context.directory / "summary.json", result)
    return result


def _regional_manifest(manifest_path: Path, region: str) -> dict:
    """The campaign manifest, refused unless it is a continuous one that has ``region``."""
    manifest = load(manifest_path)
    if manifest["schema_version"] == 3:
        raise ValueError("Ephemeral campaigns run one round per node; use 'onboarding round'")
    if region not in manifest["regions"]:
        raise ValueError("Unknown campaign region")
    return manifest


@dataclass(frozen=True)
class RegionOptions:
    """execute()'s command-line choices; a ``None`` setting keeps the campaign's (or checkpoint's).

    ``resume`` continues an existing region checkpoint; without it one must not exist.
    """

    concurrency: int | None = None
    processes: int | None = None
    request_timeout: float | None = None
    resume: bool = False


async def execute(
    manifest_path: Path,
    region: str,
    proxy_factory: Callable,
    options: RegionOptions | None = None,
    *,
    stop: asyncio.Event | None = None,
    clock=None,
    run_country=run,
) -> dict:
    options = options or RegionOptions()
    manifest = _regional_manifest(manifest_path, region)
    effective = settings(manifest["settings"])
    directory = manifest_path.parent / "regions" / region
    state_path = directory / "state.json"
    clock = clock or Clock()
    stop = stop if stop is not None else asyncio.Event()
    end = moment(regional_window(manifest, region)["ends_at"]).timestamp()
    with lock(directory / ".lock"):
        state, effective = _open_region(state_path, manifest, region, options, effective)
        if state["status"] in ("completed", "closed"):
            return status(state, clock.now())
        save = functools.partial(save_checkpoint, state_path, state, clock)
        recover_running(state, directory, clock.now())
        state["active"] = None
        state["status"] = "armed"
        save()
        context = JobContext(
            manifest,
            region,
            directory,
            effective,
            state,
            save,
            proxy_factory,
            run_country,
            stop,
            clock,
            end,
            end,
        )
        return await _drive(context)
