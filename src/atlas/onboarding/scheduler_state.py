"""A region's checkpoint document: a new state, its validation, crash recovery and status.

Split from scheduler.py, which re-exports these names for its callers."""

from __future__ import annotations

import time
from pathlib import Path

from .files import fingerprint, read_json
from .manifest import moment, regional_window, settings, timestamp

# "no_list" (ephemeral rounds): the provider has no login or no exits for the country.
TERMINAL = {"completed", "interrupted", "failed", "deadline", "not_started_deadline", "no_list"}


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


def recover_running(state: dict, directory: Path, now: float) -> None:
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


def read_checkpoint(state_path: Path, manifest: dict, region: str, resume: bool) -> dict | None:
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


def region_settings(effective: dict, state: dict | None, overrides: tuple) -> dict:
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
