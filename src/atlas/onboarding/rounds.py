"""One hourly round of one region on a fresh node, for ephemeral (version 3) campaigns.

The round runs its region's countries with the regional census settings, to their natural
end: no request cap and no deadline. Its low-availability countries (a low cap in their pace,
onboarding.country_pace) run first, side by side: at most country_pace.LOW_PARALLEL at once,
together within the round's concurrency (LOWAVAIL-PACE-2). The others then run one at a time,
the smallest pace first (country_pace.smallest_first, DAILY-ORDER-1), so a big country's burst
cannot make the gateway refuse the small ones after it; without paces, in manifest order. The
checkpoint keeps its jobs in manifest order whatever order they ran in.
A daily test's round (a manifest with `due_countries`) starts no country SOFT_LIMIT after its
hour or later (LOWAVAIL-PACE-3): those are `not_reached`, never failed, so its droplet stays
under about 30 minutes (each country ends within its time budget, daily.pace). Its checkpoint
is ``regions/<R>/rounds/<NN>/round.json``, next to the country folders it owns.
"""

from __future__ import annotations

import asyncio
import functools
import gzip
import json
import math
from collections.abc import Callable
from pathlib import Path

from ..runner import run
from . import country_pace
from .export import verify_chunk
from .files import fingerprint, lock, read_json
from .manifest import load, moment, region_countries, settings
from .scheduler import Clock, JobContext, job_directory, run_job, save_checkpoint

EARLY_START_SECONDS = 300
SOFT_LIMIT = 25 * 60  # a daily test's round starts no country from HH:25 on
# "no_list": the provider has no login or no exits for the country; complete with that reason.
# "not_reached": a daily test's country its round did not start before SOFT_LIMIT; complete.
NOT_REACHED = "not_reached"
TERMINAL = {"completed", "interrupted", "failed", "not_started_deadline", "no_list", NOT_REACHED}


class RoundDataError(ValueError):
    """A verification finding whose message is fixed text, safe to report."""


class RoundRefused(ValueError):
    """A round refused before or instead of running; its message is fixed text, safe to print."""


def round_directory(manifest_path: Path, region: str, index: int) -> Path:
    return manifest_path.parent / "regions" / region / "rounds" / f"{index:02d}"


def _ephemeral(manifest_path: Path, region: str, index: int) -> dict:
    manifest = load(manifest_path)
    if manifest["schema_version"] != 3:
        raise RoundRefused("One-round execution requires an ephemeral (version 3) manifest")
    if region not in manifest["regions"]:
        raise RoundRefused("Unknown campaign region")
    if type(index) is not int or not 0 <= index < len(manifest["rounds"]):
        raise RoundRefused("Round must be an hourly index from 0 to 23")
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
        "jobs": [
            {"round": index, "country": country, "scheduled_at": scheduled, "status": "pending"}
            for country in region_countries(manifest, region)
        ],
    }


def validate_round(state: dict, manifest: dict, region: str, index: int) -> None:
    expected = new_round(manifest, region, index)
    keys = (
        "schema_version",
        "kind",
        "campaign_id",
        "region",
        "round",
        "scheduled_at",
        "campaign_sha256",
    )
    if any(state.get(key) != expected[key] for key in keys):
        raise RoundRefused("Round checkpoint does not match this campaign, region and round")
    settings(state["settings"])
    jobs = [(j["round"], j["country"], j["scheduled_at"]) for j in state["jobs"]]
    if jobs != [(j["round"], j["country"], j["scheduled_at"]) for j in expected["jobs"]]:
        raise RoundRefused("Round checkpoint country queue differs from the campaign")


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


async def _run_one(context: JobContext, job: dict) -> None:
    """Run one country, or mark it ``no_list`` without traffic when it has no proxy."""
    if context.proxy_factory(job["country"]) is None:
        job.update(status="no_list", stop_reason="no_list")
        context.save()
        return
    await run_job(context, job)


async def _run_low(context: JobContext, jobs: list[dict]) -> None:
    """Run the low-availability countries side by side, country_pace.low_width at once (each
    within its share of the round's concurrency, scheduler._census_config). The first error
    stops the others (interrupted) and is re-raised."""
    width = country_pace.low_width(context.manifest, context.effective["concurrency"])
    slots = asyncio.Semaphore(width)

    async def one(job: dict) -> None:
        async with slots:
            if not context.stop.is_set():
                await _run_one(context, job)

    try:
        async with asyncio.TaskGroup() as group:
            for job in jobs:
                group.create_task(one(job))
    except BaseExceptionGroup as errors:
        raise errors.exceptions[0] from None


async def _run_round(context: JobContext, scheduled: float) -> None:
    """Wait for the round's hour, run its low-availability countries side by side, then the
    others smallest pace first (a daily test's until SOFT_LIMIT), then leave every job
    terminal."""
    state, save, stop = context.state, context.save, context.stop
    soft = scheduled + SOFT_LIMIT if context.manifest.get("due_countries") else math.inf
    try:
        await _wait_until(state, save, scheduled, context.clock, stop)
        low = [j for j in state["jobs"] if country_pace.low(context.manifest, j["country"])]
        if low:
            await _run_low(context, low)
        for job in country_pace.smallest_first(context.manifest, state["jobs"]):
            if stop.is_set():
                break
            if job["status"] == "pending" and context.clock.now() >= soft:
                job.update(status=NOT_REACHED, stop_reason="soft_limit")
            elif job["status"] == "pending":
                await _run_one(context, job)
        state["status"] = "stopped" if stop.is_set() else "completed"
    except BaseException:
        state["status"] = "failed"
        raise
    finally:
        _close(state)
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
    an error, which is re-raised after the checkpoint records ``failed``. A country whose
    ``proxy_factory`` returns None (no provider login or exits) is ``no_list``, without traffic.
    """
    manifest = _ephemeral(manifest_path, region, round_index)
    directory = manifest_path.parent / "regions" / region
    path = round_directory(manifest_path, region, round_index) / "round.json"
    clock = clock or Clock()
    stop = stop if stop is not None else asyncio.Event()
    scheduled = moment(manifest["rounds"][round_index]).timestamp()
    with lock(directory / ".lock"):
        if path.exists():
            raise RoundRefused("This round already has a checkpoint; a replay is refused")
        if clock.now() < scheduled - EARLY_START_SECONDS:
            raise RoundRefused("A round may be armed at most 300 seconds before it is scheduled")
        state = new_round(manifest, region, round_index)
        save = functools.partial(save_checkpoint, path, state, clock)
        save()
        context = JobContext(
            manifest,
            region,
            directory,
            state["settings"],
            state,
            save,
            proxy_factory,
            run_country,
            stop,
            clock,
            math.inf,
            None,
        )
        await _run_round(context, scheduled)
    return state


def _country_entry(job: dict, measured: dict | None) -> dict:
    measured = measured or {}
    return {
        "country": job["country"],
        "status": job["status"],
        "stop_reason": job.get("stop_reason"),
        "requests": measured.get("completed", 0),
        "successes": measured.get("successes", 0),
        "errors": measured.get("total_errors", 0),
        # Answer classes (answers.py); 0 for a measurement without them.
        "rate_limited": sum((measured.get("rate_limited") or {}).values()),
        "no_inventory": measured.get("no_inventory") or 0,
        "other_country": measured.get("other_country") or 0,
        "observations": 0,
        "unique_ips": measured.get("unique_ips"),
        "bytes_up": measured.get("bytes_up", 0),
        "bytes_down": measured.get("bytes_down", 0),
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
    if job["status"] in ("no_list", NOT_REACHED):
        return _country_entry(job, None), []
    try:
        measured = read_json(folder / "summary.json")["measurement"] or {}
        entry = _country_entry(job, measured)
        entry["observations"], unique = _verify_chunks(folder, job)
    except (OSError, ValueError, KeyError) as exc:
        # Library messages can quote file paths or row values, so only fixed text is reported.
        reason = (
            str(exc)
            if isinstance(exc, RoundDataError)
            else f"failed verification ({type(exc).__name__})"
        )
        return _country_entry(job, None), [f"{label}: {reason}"]
    entry["unique_ips"] = unique
    issues = [] if job["status"] == "completed" else [f"{label}: {job['status']}"]
    # Other-country exits are successes, never observations. A stopped country's requests still
    # in flight are cancelled; only the finished ones reconcile (DAILY-BUDGET-STOP-1).
    in_flight = measured.get("cancelled") if job["status"] == "interrupted" else 0
    if (
        entry["observations"] + entry["other_country"] != measured.get("successes")
        or entry["observations"] != job.get("observations")
        or unique != measured.get("unique_ips")
        or measured.get("completed")
        != measured.get("successes", 0) + measured.get("total_errors", 0)
        or measured.get("cancelled") != in_flight
    ):
        issues.append(f"{label}: measurements do not reconcile")
    return entry, issues


def _overlapping(manifest: dict, jobs: list[dict]) -> set[str]:
    """The countries that are not low availability and started before every country started
    earlier had finished, in the order they started (DAILY-ORDER-1: not the manifest's)."""
    found, previous_end = set(), None
    ran = [j for j in jobs if j["status"] in TERMINAL and j.get("started_at")]
    for job in sorted(ran, key=lambda j: moment(j["started_at"])):
        low = country_pace.low(manifest, job["country"])
        if not low and previous_end and moment(job["started_at"]) < previous_end:
            found.add(job["country"])
        if job.get("finished_at"):
            finished = moment(job["finished_at"])
            previous_end = max(previous_end, finished) if previous_end else finished
    return found


def verify_round(manifest_path: Path, region: str, round_index: int) -> dict:
    """Verify one collected round without writing anything; the result holds no IPs.

    ``complete`` is true when every country completed (or has no provider list, or a daily
    test's soft limit left it not reached) and its
    chunks (SHA-256, rows, time bounds up to the country's ``finished_at``) and counters
    reconcile. A checkpoint that does
    not belong to this campaign, region and round raises ``RoundRefused``.
    """
    manifest = _ephemeral(manifest_path, region, round_index)
    folder = round_directory(manifest_path, region, round_index)
    state = read_json(folder / "round.json")
    validate_round(state, manifest, region, round_index)
    directory = manifest_path.parent / "regions" / region
    countries, issues = [], []
    overlapping = _overlapping(manifest, state["jobs"])
    if state["status"] not in ("completed", "stopped", "failed"):
        issues.append(f"round is {state['status']}")
    for job in state["jobs"]:
        if job["status"] not in TERMINAL:
            issues.append(f"{job['country']}: {job['status']}")
            countries.append(_country_entry(job, None))
            continue
        if job["country"] in overlapping:
            issues.append(f"{job['country']}: overlaps the previous country")
        entry, problems = _verify_country(directory, job)
        countries.append(entry)
        issues.extend(problems)
    return {
        "region": region,
        "round": round_index,
        "countries": countries,
        "rows": sum(c["observations"] for c in countries),
        "complete": not issues,
        "issues": issues,
    }
