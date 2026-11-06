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
from .files import atomic_json, fingerprint, lock, read_json
from .manifest import load, moment, regional_window, settings, timestamp
from .writer import ObservationWriter, SummarySink

# "no_list" (ephemeral rounds): the provider has no login or no exits for the country.
TERMINAL = {"completed", "interrupted", "failed", "deadline", "not_started_deadline", "no_list"}


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


def job_directory(directory: Path, job: dict) -> Path:
    return directory / "rounds" / f"{job['round']:02d}" / job["country"].lower()


def new_state(manifest: dict, region: str, effective: dict) -> dict:
    return {
        "schema_version": 1,
        "kind": "campaign",
        "campaign_id": manifest["campaign_id"],
        "region": region,
        "campaign_sha256": fingerprint(manifest),
        "settings": effective,
        "status": "armed",
        "ends_at": regional_window(manifest, region)["ends_at"],
        "active": None,
        "progress": {},
        "jobs": [
            {"round": index, "country": country, "scheduled_at": scheduled, "status": "pending"}
            for index, scheduled in enumerate(regional_window(manifest, region)["rounds"])
            for country in manifest["regions"][region]
        ],
    }


def validate_state(state: dict, manifest: dict, region: str) -> None:
    if (
        state.get("schema_version") != 1
        or state.get("region") != region
        or state.get("campaign_sha256") != fingerprint(manifest)
        or state.get("campaign_id") != manifest["campaign_id"]
        or state.get("ends_at") != regional_window(manifest, region)["ends_at"]
    ):
        raise ValueError("Checkpoint does not match this campaign and region")
    settings(state["settings"])
    expected = [
        (i, c, at)
        for i, at in enumerate(regional_window(manifest, region)["rounds"])
        for c in manifest["regions"][region]
    ]
    actual = [(j["round"], j["country"], j["scheduled_at"]) for j in state["jobs"]]
    if actual != expected or any(
        j["status"] not in TERMINAL | {"pending", "running"} for j in state["jobs"]
    ):
        raise ValueError("Checkpoint country queue differs from the campaign")


def status(state: dict, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    jobs = state["jobs"]
    queue = [
        j for j in jobs if j["status"] == "pending" and moment(j["scheduled_at"]).timestamp() <= now
    ]
    rounds = []
    for index in range(24):
        group = [j for j in jobs if j["round"] == index]
        starts = [j["started_at"] for j in group if j.get("started_at")]
        ends = [j["finished_at"] for j in group if j.get("finished_at")]
        complete = all(j["status"] in ("completed", "no_list") for j in group)
        rounds.append(
            {
                "round": index,
                "scheduled_at": group[0]["scheduled_at"],
                "completed": complete,
                "started_at": min(starts) if starts else None,
                "finished_at": max(ends) if complete and ends else None,
                "duration_seconds": (moment(max(ends)) - moment(min(starts))).total_seconds()
                if complete and starts and ends
                else None,
                "delay_seconds": (
                    moment(min(starts)) - moment(group[0]["scheduled_at"])
                ).total_seconds()
                if starts
                else None,
            }
        )
    return {
        "campaign_id": state["campaign_id"],
        "region": state["region"],
        "status": state["status"],
        "settings": state["settings"],
        "ends_at": state["ends_at"],
        "active": state["active"],
        "queue_depth": len(queue),
        "progress": state["progress"],
        "observations": sum(j.get("observations", 0) for j in jobs),
        "requests": sum(j.get("requests", 0) for j in jobs),
        "errors": sum(j.get("errors", 0) for j in jobs),
        "completed_countries": sum(j["status"] == "completed" for j in jobs),
        "unfinished_countries": sum(j["status"] not in ("completed", "no_list") for j in jobs),
        "peak_memory_mb": state.get("peak_memory_mb"),
        "rounds": rounds,
        "updated_at": state.get("updated_at"),
    }


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


def _census_config(context: JobContext, proxy) -> RunConfig:
    """One country's census run config; ``answer_classes`` come from the network's grammar."""
    effective = dict(context.effective)
    classes = (
        answer_classes(context.manifest["provider"])
        if effective.pop("answer_classes", False)
        else None
    )
    return RunConfig(
        "generic_http",
        proxy,
        requests=None,
        stop_rule="unique_ratio",
        runner_location=context.region,
        target_url=context.manifest["target_url"],
        answer_classes=classes,
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
        except Exception as exc:
            checkpoint_error[0] = exc
            country_stop.set()

    watcher = asyncio.create_task(watch())
    summary = None
    error = None
    try:
        config = _census_config(context, _checked_proxy(context, job))
        options = RunOptions(
            stop=country_stop,
            deadline=context.deadline,
            aggregate_ips=False,
            wall_clock=clock.now,
            observation_consumer=writer.observe,
            progress=progress,
            progress_interval=5,
        )
        summary = await context.run_country(config, sink, options)
    except BaseException as exc:
        error = exc
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        try:
            await writer.close()
        except Exception as exc:
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


def _recover_running(state: dict, directory: Path, now: float) -> None:
    """A process interruption cannot replay a partially measured country."""
    for job in state["jobs"]:
        if job["status"] != "running":
            continue
        summary_path = job_directory(directory, job) / "summary.json"
        if summary_path.exists():
            saved = read_json(summary_path)
            for key in (
                "status",
                "finished_at",
                "observations",
                "requests",
                "errors",
                "duration_seconds",
                "stop_reason",
            ):
                job[key] = saved[key]
            continue
        folder = job_directory(directory, job)
        committed = sum(read_json(p)["rows"] for p in folder.glob("observations-*.jsonl.meta.json"))
        counters = state.get("progress", {})
        job.update(
            status="interrupted",
            finished_at=timestamp(now),
            observations=committed,
            requests=counters.get("requests", 0),
            errors=counters.get("requests", 0) - counters.get("successes", 0),
            counters_partial=True,
        )


def _read_checkpoint(state_path: Path, manifest: dict, region: str, resume: bool) -> dict | None:
    """The saved state to resume, or ``None`` for a new region."""
    if state_path.exists():
        if not resume:
            raise ValueError("Region already exists; use --resume")
        state = read_json(state_path)
        validate_state(state, manifest, region)
        return state
    if resume:
        raise ValueError("No region checkpoint exists to resume")
    return None


def _region_settings(effective: dict, state: dict | None, overrides: tuple) -> dict:
    """The checkpoint's settings on resume (else ``effective``) with the command-line overrides."""
    if state is not None:
        effective = settings(state["settings"])
    for key, override in overrides:
        if override is not None:
            if state is not None and override != effective[key]:
                raise ValueError("Resume must preserve the original regional settings")
            effective[key] = override
    settings(effective)
    return effective


def _open_region(
    state_path: Path, manifest: dict, region: str, options: RegionOptions, effective: dict
) -> tuple[dict, dict]:
    """The region's ``(state, settings)``: its checkpoint on resume, else a new state."""
    state = _read_checkpoint(state_path, manifest, region, options.resume)
    overrides = (
        ("concurrency", options.concurrency),
        ("timeout_seconds", options.request_timeout),
        ("processes", options.processes),
    )
    effective = _region_settings(effective, state, overrides)
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
        _recover_running(state, directory, clock.now())
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
