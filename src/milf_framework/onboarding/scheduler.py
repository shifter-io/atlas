"""A durable hourly FIFO and a single country executor per region."""

from __future__ import annotations

import asyncio
import os
import resource
import sys
import time
from collections.abc import Callable
from pathlib import Path

from ..models import RunConfig
from ..runner import run
from .files import atomic_json, fingerprint, lock, read_json
from .manifest import load, moment, settings, timestamp
from .writer import ObservationWriter, SummarySink

TERMINAL = {"completed", "interrupted", "failed", "deadline", "not_started_deadline"}


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
        "ends_at": manifest["ends_at"],
        "active": None,
        "progress": {},
        "jobs": [
            {"round": index, "country": country, "scheduled_at": scheduled, "status": "pending"}
            for index, scheduled in enumerate(manifest["rounds"])
            for country in manifest["regions"][region]
        ],
    }


def validate_state(state: dict, manifest: dict, region: str) -> None:
    if (
        state.get("schema_version") != 1
        or state.get("region") != region
        or state.get("campaign_sha256") != fingerprint(manifest)
        or state.get("campaign_id") != manifest["campaign_id"]
        or state.get("ends_at") != manifest["ends_at"]
    ):
        raise ValueError("Checkpoint does not match this campaign and region")
    settings(state["settings"])
    expected = [
        (i, c, at) for i, at in enumerate(manifest["rounds"]) for c in manifest["regions"][region]
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
        complete = all(j["status"] == "completed" for j in group)
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
        "unfinished_countries": sum(j["status"] != "completed" for j in jobs),
        "peak_memory_mb": state.get("peak_memory_mb"),
        "rounds": rounds,
        "updated_at": state.get("updated_at"),
    }


async def execute(
    manifest_path: Path,
    region: str,
    proxy_factory: Callable,
    *,
    concurrency: int | None = None,
    processes: int | None = None,
    request_timeout: float | None = None,
    resume: bool = False,
    stop: asyncio.Event | None = None,
    clock=None,
    run_country=run,
) -> dict:
    manifest = load(manifest_path)
    if region not in manifest["regions"]:
        raise ValueError("Unknown campaign region")
    effective = settings(manifest["settings"])
    directory = manifest_path.parent / "regions" / region
    state_path = directory / "state.json"
    clock = clock or Clock()
    stop = stop if stop is not None else asyncio.Event()
    end = moment(manifest["ends_at"]).timestamp()
    with lock(directory / ".lock"):
        if state_path.exists():
            if not resume:
                raise ValueError("Region already exists; use --resume")
            state = read_json(state_path)
            validate_state(state, manifest, region)
            effective = settings(state["settings"])
        else:
            if resume:
                raise ValueError("No region checkpoint exists to resume")
            state = None
        for key, override in (("concurrency", concurrency), ("timeout_seconds", request_timeout),
                              ("processes", processes)):
            if override is not None:
                if state is not None and override != effective[key]:
                    raise ValueError("Resume must preserve the original regional settings")
                effective[key] = override
        settings(effective)
        if state is None:
            state = new_state(manifest, region, effective)
        if state["status"] in ("completed", "closed"):
            return status(state, clock.now())

        def save():
            state["pid"] = os.getpid()
            state["updated_at"] = timestamp(clock.now())
            state["peak_memory_mb"] = peak_memory_mb()
            atomic_json(state_path, state)

        # A process interruption cannot replay a partially measured country.
        for job in state["jobs"]:
            if job["status"] == "running":
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
                else:
                    folder = job_directory(directory, job)
                    committed = sum(
                        read_json(p)["rows"] for p in folder.glob("observations-*.jsonl.meta.json")
                    )
                    counters = state.get("progress", {})
                    job.update(
                        status="interrupted",
                        finished_at=timestamp(clock.now()),
                        observations=committed,
                        requests=counters.get("requests", 0),
                        errors=counters.get("requests", 0) - counters.get("successes", 0),
                        counters_partial=True,
                    )
        state["active"] = None
        state["status"] = "armed"
        save()
        try:
            for job in state["jobs"]:
                if job["status"] in TERMINAL:
                    continue
                scheduled = moment(job["scheduled_at"]).timestamp()
                while clock.now() < scheduled and clock.now() < end and not stop.is_set():
                    await clock.sleep(min(0.25, scheduled - clock.now(), end - clock.now()))
                    if clock.now() - moment(state["updated_at"]).timestamp() >= 5:
                        save()
                if stop.is_set() or clock.now() >= end:
                    break
                began = clock.now()
                job.update(
                    status="running",
                    started_at=timestamp(began),
                    delay_seconds=max(0.0, began - scheduled),
                )
                state["active"] = {
                    "round": job["round"],
                    "country": job["country"],
                    "started_at": job["started_at"],
                }
                state["status"] = "running"
                state["progress"] = {}
                save()
                folder = job_directory(directory, job)
                writer = ObservationWriter(
                    folder,
                    country=job["country"],
                    scheduled=job["scheduled_at"],
                    started=job["started_at"],
                    deadline=end,
                )
                sink = SummarySink()
                country_stop = asyncio.Event()
                checkpoint_error = [None]

                async def watch(country_stop=country_stop):
                    while not stop.is_set() and clock.now() < end:
                        await clock.sleep(min(0.05, end - clock.now()))
                    country_stop.set()

                async def progress(
                    counters,
                    writer=writer,
                    country_stop=country_stop,
                    checkpoint_error=checkpoint_error,
                ):
                    try:
                        writer.check()
                        state["progress"] = dict(counters) | {"observations_saved": writer.rows}
                        save()
                    except Exception as exc:
                        checkpoint_error[0] = exc
                        country_stop.set()

                watcher = asyncio.create_task(watch())
                summary = None
                error = None
                try:
                    proxy = proxy_factory(job["country"])
                    metadata = proxy.public()
                    if (
                        metadata.get("provider") != manifest["provider"]
                        or metadata.get("product") != manifest["product"]
                        or str(metadata.get("requested_country", "")).lower()
                        != job["country"].lower()
                    ):
                        raise ValueError("Provider must target and label the configured country")
                    config = RunConfig(
                        "generic_http",
                        proxy,
                        requests=None,
                        stop_rule="unique_ratio",
                        runner_location=region,
                        target_url=manifest["target_url"],
                        **effective,
                    )
                    summary = await run_country(
                        config,
                        sink,
                        stop=country_stop,
                        deadline=end,
                        aggregate_ips=False,
                        wall_clock=clock.now,
                        observation_consumer=writer.observe,
                        progress=progress,
                        progress_interval=5,
                    )
                except BaseException as exc:
                    error = exc
                finally:
                    watcher.cancel()
                    await asyncio.gather(watcher, return_exceptions=True)
                    try:
                        await writer.close()
                    except Exception as exc:
                        error = error or exc
                error = error or checkpoint_error[0]
                finished = clock.now()
                if error is not None:
                    job_status = (
                        "interrupted" if isinstance(error, asyncio.CancelledError) else "failed"
                    )
                elif summary is not None and summary.status == "completed":
                    # Successful measurement stays complete if local file finalization
                    # crosses the campaign deadline.
                    job_status = "completed"
                elif finished >= end or (summary and summary.stop_reason == "deadline"):
                    job_status = "deadline"
                else:
                    job_status = "interrupted"
                measurements = summary or getattr(sink, "summary", None)
                job.update(
                    status=job_status,
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
                        "campaign_id": manifest["campaign_id"],
                        "region": region,
                        **job,
                        "settings": effective,
                        "chunks": writer.chunks,
                        "peak_queue": writer.peak_queue,
                        "peak_memory_mb": peak_memory_mb(),
                        "measurement": measurements.to_dict() if measurements else None,
                    },
                )
                state["active"] = None
                save()
                if error is not None:
                    raise error
            if not stop.is_set() and all(j["status"] in TERMINAL for j in state["jobs"]):
                state["status"] = "waiting_for_deadline"
                save()
                while clock.now() < end and not stop.is_set():
                    await clock.sleep(min(0.25, end - clock.now()))
                    if clock.now() - moment(state["updated_at"]).timestamp() >= 5:
                        save()
            expired = clock.now() >= end
            if expired:
                for job in state["jobs"]:
                    if job["status"] == "pending":
                        job["status"] = "not_started_deadline"
            state["status"] = "closed" if expired else "stopped" if stop.is_set() else "completed"
        except BaseException:
            state["status"] = "failed"
            save()
            raise
        state["active"] = None
        save()
        result = status(state, clock.now())
        atomic_json(directory / "summary.json", result)
        return result
