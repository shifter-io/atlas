"""Build each region's 24-round `state.json` from a finished campaign's collected rounds.

A campaign cell is one network, region and hour. Its data is the round the harvest pulled
(`regions/<R>/rounds/<NN>/round.json`); a cell without one is judged from the hour ledger's
droplet records. The merged region is an ordinary closed onboarding checkpoint, so
`onboarding export` reads it unchanged.
"""

from __future__ import annotations

from pathlib import Path

from ..onboarding.files import atomic_json, lock, read_json
from ..onboarding.manifest import load, moment, settings
from ..onboarding.rounds import round_directory, validate_round
from ..onboarding.scheduler import new_state, status, validate_state
from .contracts import CampaignSpec, DropletRecord, HourRecord, hour_path, spec_path

HOURS = 24
# Droplet states that can still produce a round; any other state without a pulled
# `round.json` means the worker failed on our side (create, assign, boot, start or collect).
LIVE = frozenset({"requested", "assigned", "booting", "starting", "armed", "running",
                  "finished"})
ROUND_TERMINAL = frozenset({"completed", "stopped", "failed"})


class CampaignNotFinished(ValueError):
    """A cell is still pending or running, so the campaign cannot be merged yet."""


def load_spec(root: Path) -> CampaignSpec:
    return CampaignSpec.from_json(spec_path(root).read_text())


def manifest_path(root: Path, network: str) -> Path:
    """The network's only v3 manifest, `networks/<net>/residential/<mcid>/campaign.json`."""
    found = sorted((root / "networks" / network / "residential").glob("*/campaign.json"))
    if len(found) != 1:
        raise ValueError(f"{network}: expected exactly one campaign manifest")
    return found[0]


def ledger_droplets(root: Path) -> dict[tuple[str, str, int], list[DropletRecord]]:
    """Droplet records per (network, region, hour), in ledger order, from every hour record."""
    cells: dict[tuple[str, str, int], list[DropletRecord]] = {}
    for hour in range(HOURS):
        path = hour_path(root, hour)
        if path.exists():
            for droplet in HourRecord.from_dict(read_json(path)).droplets:
                key = (droplet.network, droplet.region, droplet.hour)
                cells.setdefault(key, []).append(droplet)
    return cells


def ledger_cell(droplets: list[DropletRecord] | None) -> str:
    """The status of a cell that has no pulled round: pending, running or failed_ours."""
    if not droplets:
        return "pending"
    return "running" if any(d.status in LIVE for d in droplets) else "failed_ours"


def read_round(path: Path, manifest: dict, region: str, hour: int) -> dict | None:
    """The validated `round.json` of one cell, or None when the round was never pulled."""
    file = round_directory(path, region, hour) / "round.json"
    if not file.exists():
        return None
    state = read_json(file)
    validate_round(state, manifest, region, hour)
    return state


def _unstarted_jobs(manifest: dict, region: str, hour: int) -> list[dict]:
    scheduled = manifest["rounds"][hour]
    return [{"round": hour, "country": country, "scheduled_at": scheduled,
             "status": "not_started_deadline"} for country in manifest["regions"][region]]


def build_region(path: Path, manifest: dict, network: str, region: str,
                 droplets: dict) -> dict:
    """The closed 24-round checkpoint of one region; raises on an unfinished cell."""
    jobs, rounds = [], []
    for hour in range(HOURS):
        state = read_round(path, manifest, region, hour)
        if state is None:
            cell = ledger_cell(droplets.get((network, region, hour)))
        else:
            cell = "complete" if state["status"] in ROUND_TERMINAL else "running"
        if cell in ("pending", "running"):
            raise CampaignNotFinished(f"{network} {region} h{hour:02d} is {cell}; not merged")
        if state is None:
            jobs.extend(_unstarted_jobs(manifest, region, hour))
        else:
            jobs.extend(state["jobs"])
            rounds.append(state)
    merged = new_state(manifest, region, settings(manifest["settings"]))
    peaks = [r["peak_memory_mb"] for r in rounds if r.get("peak_memory_mb") is not None]
    merged.update(status="closed", jobs=jobs, peak_memory_mb=max(peaks, default=None),
                  updated_at=max((r["updated_at"] for r in rounds), default=manifest["ends_at"]))
    validate_state(merged, manifest, region)
    return merged


def merge_network(root: Path, network: str) -> list[dict]:
    """Merge every spec region of one network; returns the regional summaries.

    Every cell must be finished: a pulled round in a terminal state, or a ledger
    `failed_ours` cell, whose countries become `not_started_deadline` jobs at the manifest's
    `scheduled_at`. A pending or running cell raises `CampaignNotFinished` before any write.
    """
    spec = load_spec(root)
    if network not in spec.networks:
        raise ValueError("Network is not part of this campaign")
    path = manifest_path(root, network)
    manifest = load(path)
    if manifest["schema_version"] != 3:
        raise ValueError("Campaign merge requires ephemeral (version 3) manifests")
    droplets = ledger_droplets(root)
    merged = [build_region(path, manifest, network, region, droplets) for region in spec.regions]
    summaries = []
    for state in merged:
        directory = path.parent / "regions" / state["region"]
        summary = status(state, moment(state["ends_at"]).timestamp())
        with lock(directory / ".lock"):
            atomic_json(directory / "state.json", state)
            atomic_json(directory / "summary.json", summary)
        summaries.append(summary)
    return summaries
