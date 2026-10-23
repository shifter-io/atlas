"""One hourly round of one region on a fresh node, for ephemeral (version 3) campaigns.

The round runs its region's countries in manifest order with the regional census settings,
to their natural end: no request cap and no deadline. Its checkpoint is
``regions/<R>/rounds/<NN>/round.json``, next to the country folders it owns.
"""

from __future__ import annotations

import asyncio
import functools
import gzip
import json
import math
import os
from collections.abc import Callable
from pathlib import Path

from ..runner import run
from .export import verify_chunk
from .files import atomic_json, fingerprint, lock, read_json
from .manifest import load, moment, settings, timestamp
from .scheduler import Clock, JobContext, job_directory, peak_memory_mb, run_job

EARLY_START_SECONDS = 300
TERMINAL = {"completed", "interrupted", "failed", "not_started_deadline"}


class RoundDataError(ValueError):
    """A verification finding whose message is fixed text, safe to report."""


def round_directory(manifest_path: Path, region: str, index: int) -> Path:
    return manifest_path.parent / "regions" / region / "rounds" / f"{index:02d}"


def _ephemeral(manifest_path: Path, region: str, index: int) -> dict:
    manifest = load(manifest_path)
    if manifest["schema_version"] != 3:
        raise ValueError("One-round execution requires an ephemeral (version 3) manifest")
    if region not in manifest["regions"]:
        raise ValueError("Unknown campaign region")
    if type(index) is not int or not 0 <= index < len(manifest["rounds"]):
        raise ValueError("Round must be an hourly index from 0 to 23")
    return manifest


def new_round(manifest: dict, region: str, index: int) -> dict:
    scheduled = manifest["rounds"][index]
    return {
        "schema_version": 1,
        "kind": "round",
        "campaign_id": manifest["campaign_id"],
        "region": region,
        "round": index,
        "scheduled_at": scheduled,
        "campaign_sha256": fingerprint(manifest),
        "settings": settings(manifest["settings"]),
        "status": "armed",
        "active": None,
        "progress": {},
        "jobs": [{"round": index, "country": country, "scheduled_at": scheduled,
                  "status": "pending"} for country in manifest["regions"][region]],
    }


def validate_round(state: dict, manifest: dict, region: str, index: int) -> None:
    expected = new_round(manifest, region, index)
    keys = ("schema_version", "kind", "campaign_id", "region", "round", "scheduled_at",
            "campaign_sha256")
    if any(state.get(key) != expected[key] for key in keys):
        raise ValueError("Round checkpoint does not match this campaign, region and round")
    settings(state["settings"])
    jobs = [(j["round"], j["country"], j["scheduled_at"]) for j in state["jobs"]]
    if jobs != [(j["round"], j["country"], j["scheduled_at"]) for j in expected["jobs"]]:
        raise ValueError("Round checkpoint country queue differs from the campaign")


def _save(path: Path, state: dict, clock) -> None:
    state["pid"] = os.getpid()
    state["updated_at"] = timestamp(clock.now())
    state["peak_memory_mb"] = peak_memory_mb()
    atomic_json(path, state, indent=None)


def _close(state: dict) -> None:
    """Leave every job terminal: queued ones never started, a stray active one failed."""
    for job in state["jobs"]:
        if job["status"] == "pending":
            job["status"] = "not_started_deadline"
        elif job["status"] == "running":
            job["status"] = "failed"
    state["active"] = None


async def _wait_until(state: dict, save: Callable, scheduled: float, clock, stop) -> None:
    while clock.now() < scheduled and not stop.is_set():
        await clock.sleep(min(0.25, scheduled - clock.now()))
        if clock.now() - moment(state["updated_at"]).timestamp() >= 5:
            save()


async def execute_round(
    manifest_path: Path,
    region: str,
    round_index: int,
    proxy_factory: Callable,
    *,
    stop: asyncio.Event | None = None,
    clock=None,
    run_country=run,
) -> dict:
    """Run one round once and return its final ``round.json`` document.

    Refuses a non-ephemeral manifest, a replay (an existing ``round.json``) and a start more
    than 300 s before the round's ``scheduled_at``; an earlier start waits for it. A stop
    marks the active country ``interrupted`` and the rest ``not_started_deadline``; so does
    an error, which is re-raised after the checkpoint records ``failed``.
    """
    manifest = _ephemeral(manifest_path, region, round_index)
    directory = manifest_path.parent / "regions" / region
    path = round_directory(manifest_path, region, round_index) / "round.json"
    clock = clock or Clock()
    stop = stop if stop is not None else asyncio.Event()
    scheduled = moment(manifest["rounds"][round_index]).timestamp()
    with lock(directory / ".lock"):
        if path.exists():
            raise ValueError("This round already has a checkpoint; a replay is refused")
        if clock.now() < scheduled - EARLY_START_SECONDS:
            raise ValueError("A round may be armed at most 300 seconds before it is scheduled")
        state = new_round(manifest, region, round_index)
        save = functools.partial(_save, path, state, clock)
        save()
        context = JobContext(manifest, region, directory, state["settings"], state, save,
                             proxy_factory, run_country, stop, clock, math.inf, None)
        try:
            await _wait_until(state, save, scheduled, clock, stop)
            for job in state["jobs"]:
                if stop.is_set():
                    break
                await run_job(context, job)
            state["status"] = "stopped" if stop.is_set() else "completed"
        except BaseException:
            state["status"] = "failed"
            raise
        finally:
            _close(state)
            save()
    return state


def _country_entry(job: dict, measured: dict | None) -> dict:
    measured = measured or {}
    return {
        "country": job["country"], "status": job["status"],
        "stop_reason": job.get("stop_reason"), "requests": measured.get("completed", 0),
        "successes": measured.get("successes", 0), "errors": measured.get("total_errors", 0),
        "observations": 0, "unique_ips": measured.get("unique_ips"),
        "bytes_up": measured.get("bytes_up", 0), "bytes_down": measured.get("bytes_down", 0),
        "duration_seconds": job.get("duration_seconds"),
    }


def _verify_chunks(folder: Path, job: dict) -> tuple[int, int]:
    """Verify every committed chunk; return ``(rows, unique IPs)``; raise on any defect."""
    if list(folder.glob("*.partial")):
        raise RoundDataError("unfinished chunk")
    rows, unique, verified = 0, set(), set()
    for meta_path in sorted(folder.glob("observations-*.jsonl.meta.json")):
        meta = read_json(meta_path)
        name = meta.get("file")
        if not isinstance(name, str) or Path(name).name != name:
            raise RoundDataError("invalid chunk filename")
        chunk = folder / name
        if chunk.with_suffix(".meta.json") != meta_path:
            raise RoundDataError("chunk filename does not match its sidecar")
        rows += verify_chunk(chunk, meta, job, job["finished_at"])
        with gzip.open(chunk, "rt") as stream:
            unique.update(json.loads(line)["ip"] for line in stream)
        verified.add(name)
    if verified != {p.name for p in folder.glob("observations-*.jsonl.gz")}:
        raise RoundDataError("chunk without verified metadata")
    return rows, len(unique)


def _verify_country(directory: Path, job: dict) -> tuple[dict, list[str]]:
    folder = job_directory(directory, job)
    label = job["country"]
    if job["status"] == "not_started_deadline":
        return _country_entry(job, None), [f"{label}: not started"]
    try:
        measured = read_json(folder / "summary.json")["measurement"] or {}
        entry = _country_entry(job, measured)
        entry["observations"], unique = _verify_chunks(folder, job)
    except (OSError, ValueError, KeyError) as exc:
        # Library messages can quote file paths or row values, so only fixed text is reported.
        reason = (str(exc) if isinstance(exc, RoundDataError)
                  else f"failed verification ({type(exc).__name__})")
        return _country_entry(job, None), [f"{label}: {reason}"]
    entry["unique_ips"] = unique
    issues = [] if job["status"] == "completed" else [f"{label}: {job['status']}"]
    if (entry["observations"] != measured.get("successes")
            or entry["observations"] != job.get("observations")
            or unique != measured.get("unique_ips")
            or measured.get("completed") != measured.get("successes", 0)
            + measured.get("total_errors", 0)
            or measured.get("cancelled") != 0):
        issues.append(f"{label}: measurements do not reconcile")
    return entry, issues


def verify_round(manifest_path: Path, region: str, round_index: int) -> dict:
    """Verify one collected round without writing anything; the result holds no IPs.

    ``complete`` is true when every country completed and its chunks (SHA-256, rows, time
    bounds up to the country's ``finished_at``) and counters reconcile. A checkpoint that does
    not belong to this campaign, region and round raises ``ValueError``.
    """
    manifest = _ephemeral(manifest_path, region, round_index)
    folder = round_directory(manifest_path, region, round_index)
    state = read_json(folder / "round.json")
    validate_round(state, manifest, region, round_index)
    directory = manifest_path.parent / "regions" / region
    countries, issues, previous_end = [], [], None
    if state["status"] not in ("completed", "stopped", "failed"):
        issues.append(f"round is {state['status']}")
    for job in state["jobs"]:
        if job["status"] not in TERMINAL:
            issues.append(f"{job['country']}: {job['status']}")
            countries.append(_country_entry(job, None))
            continue
        if previous_end and job.get("started_at") and moment(job["started_at"]) < previous_end:
            issues.append(f"{job['country']}: overlaps the previous country")
        previous_end = moment(job["finished_at"]) if job.get("finished_at") else previous_end
        entry, problems = _verify_country(directory, job)
        countries.append(entry)
        issues.extend(problems)
    return {"region": region, "round": round_index, "countries": countries,
            "rows": sum(c["observations"] for c in countries), "complete": not issues,
            "issues": issues}
