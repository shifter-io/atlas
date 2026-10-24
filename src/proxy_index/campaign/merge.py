"""Build each region's 24-round `state.json` from a finished campaign's collected rounds.

A campaign cell is one network, region and hour. Its data is the round the harvest pulled
(`regions/<R>/rounds/<NN>/round.json`); a cell without one is judged from the hour ledger's
droplet records and paused networks. The merged region is an ordinary closed onboarding
checkpoint, so `onboarding export` reads it unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import get_args

from ..onboarding.files import atomic_json, lock, read_json
from ..onboarding.manifest import load, moment, settings
from ..onboarding.rounds import _close, round_directory, validate_round
from ..onboarding.scheduler import new_state, status, validate_state
from .contracts import (
    CampaignSpec,
    DropletRecord,
    DropletStatus,
    HourRecord,
    hour_path,
    spec_path,
)

HOURS = 24
# Droplet states that can still produce or hold a round that is not pulled yet.
LIVE = frozenset({"requested", "assigned", "booting", "starting", "armed", "running",
                  "finished"})
# A worker in one of these states never started its round: create, assign, boot or start
# failed. Read from the frozen contract; the scope suite reserves writing these for scope.py.
NEVER_STARTED = frozenset(s for s in get_args(DropletStatus) if s.endswith("_failed")) - {
    "collect_failed", "destroy_failed"}
DESTROYED = frozenset({"destroyed", "destroy_refused", "destroy_failed"})
ROUND_TERMINAL = frozenset({"completed", "stopped", "failed"})
# Notes that qualify a cell's status; merge and completeness report them verbatim.
WAITING = "data waits on a kept worker (collect_failed)"
MISSING = "round ran; data not collected"
ENDED = "round ended without closing"


class CampaignNotFinished(ValueError):
    """A cell is still pending, running or waiting for its collect, so no merge yet."""


class RoundDataMissing(ValueError):
    """A worker started its round but the round never reached the campaign root."""


def load_spec(root: Path) -> CampaignSpec:
    return CampaignSpec.from_json(spec_path(root).read_text())


def manifest_path(root: Path, network: str) -> Path:
    """The network's only v3 manifest, `networks/<net>/residential/<mcid>/campaign.json`."""
    found = sorted((root / "networks" / network / "residential").glob("*/campaign.json"))
    if len(found) != 1:
        raise ValueError(f"{network}: expected exactly one campaign manifest")
    return found[0]


@dataclass(frozen=True)
class Ledger:
    """What merge reads from the hour records: droplet records per (network, region, hour),
    in ledger order, and the (network, hour) pairs whose network was paused at launch."""

    droplets: dict[tuple[str, str, int], list[DropletRecord]]
    paused: frozenset[tuple[str, int]]

    def paused_cell(self, cell: tuple[str, str, int]) -> bool:
        network, _, hour = cell
        return (network, hour) in self.paused


def read_ledger(root: Path) -> Ledger:
    """The `Ledger` of every hour record under `root`."""
    cells: dict[tuple[str, str, int], list[DropletRecord]] = {}
    paused: set[tuple[str, int]] = set()
    for hour in range(HOURS):
        path = hour_path(root, hour)
        if path.exists():
            record = HourRecord.from_dict(read_json(path))
            paused |= {(network, hour) for network in record.paused_networks}
            for droplet in record.droplets:
                key = (droplet.network, droplet.region, droplet.hour)
                cells.setdefault(key, []).append(droplet)
    return Ledger(cells, frozenset(paused))


def _never_started(droplet: DropletRecord) -> bool:
    unstarted = droplet.armed_at is None and droplet.started_at is None
    return droplet.status in NEVER_STARTED or (droplet.status in DESTROYED and unstarted)


def cell_status(state: dict | None, droplets: list[DropletRecord] | None,
                paused: bool = False) -> tuple[str, str | None]:
    """A cell's status and qualifying note from its pulled round (or None) and its workers.

    Statuses: `pulled` (a closed round, judged by its data), `failed_ours`, `running`,
    `pending`, `uncollected` (the round ran but is not in the campaign root) and `paused`
    (no worker, and the network was paused when its hour launched: final, with no data). A
    round that is not closed while no worker is live ended without closing (killed):
    `failed_ours`.
    """
    live = any(d.status in LIVE for d in droplets or ())
    if state is not None:
        if state.get("status") in ROUND_TERMINAL:
            return ("failed_ours" if state["status"] == "failed" else "pulled"), None
        return ("running", None) if live else ("failed_ours", ENDED)
    if not droplets:
        return ("paused" if paused else "pending"), None
    if live:
        return "running", None
    if any(d.status == "collect_failed" for d in droplets):
        return "uncollected", WAITING
    if all(_never_started(d) for d in droplets):
        return "failed_ours", None
    return "uncollected", MISSING


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


def _cell_jobs(path: Path, manifest: dict, cell: tuple[str, str, int],
               ledger: Ledger) -> tuple[list[dict], dict | None]:
    """The jobs one finished cell contributes, and its round; raises for any other cell."""
    network, region, hour = cell
    state = read_round(path, manifest, region, hour)
    kind, note = cell_status(state, ledger.droplets.get(cell), ledger.paused_cell(cell))
    label = f"{network} {region} h{hour:02d}"
    if kind in ("pending", "running") or note == WAITING:
        raise CampaignNotFinished(f"{label}: {note}" if note else f"{label} is {kind}")
    if note == MISSING:
        raise RoundDataMissing(f"{label}: {note}")
    if state is None:
        return _unstarted_jobs(manifest, region, hour), None
    if note == ENDED:
        _close(state)  # the executor's own closing rule: pending -> not_started_deadline,
        # running -> failed
    return state["jobs"], state


def build_region(path: Path, manifest: dict, network: str, region: str,
                 ledger: Ledger, slots: int = HOURS) -> dict:
    """The closed 24-round checkpoint of one region; raises on an unfinished cell. Rounds past
    the spec's `slots` (a compressed test) are never launched: they close unstarted."""
    jobs, rounds = [], []
    for hour in range(HOURS):
        if hour >= slots:
            jobs.extend(_unstarted_jobs(manifest, region, hour))
            continue
        cell = (network, region, hour)
        cell_jobs, state = _cell_jobs(path, manifest, cell, ledger)
        jobs.extend(cell_jobs)
        rounds += [state] if state else []
    merged = new_state(manifest, region, settings(manifest["settings"]))
    peaks = [r["peak_memory_mb"] for r in rounds if r.get("peak_memory_mb") is not None]
    merged.update(status="closed", jobs=jobs, peak_memory_mb=max(peaks, default=None),
                  updated_at=max((r["updated_at"] for r in rounds), default=manifest["ends_at"]))
    validate_state(merged, manifest, region)
    return merged


def merge_networks(root: Path, networks: list[str] | tuple[str, ...]) -> dict[str, list[dict]]:
    """Merge every spec region of `networks`; returns the regional summaries per network.

    Every cell of every requested network is checked before anything is written. A cell is
    finished when its pulled round is closed, its round ended without closing and no worker
    is live (open jobs close as `failed` or `not_started_deadline`), or no worker ever
    started or the cell is `paused` (countries become `not_started_deadline` at the
    manifest's `scheduled_at`, with no data).
    Pending, running and `collect_failed` cells raise `CampaignNotFinished`; a started
    worker whose round is not in the root raises `RoundDataMissing`.
    """
    spec = load_spec(root)
    if not set(networks) <= set(spec.networks):
        raise ValueError("Network is not part of this campaign")
    ledger = read_ledger(root)
    built = []
    for network in networks:
        path = manifest_path(root, network)
        manifest = load(path)
        if manifest["schema_version"] != 3:
            raise ValueError("Campaign merge requires ephemeral (version 3) manifests")
        built += [(network, path, build_region(path, manifest, network, region, ledger,
                                                    spec.hours))
                  for region in spec.regions]
    summaries: dict[str, list[dict]] = {network: [] for network in networks}
    for network, path, state in built:
        directory = path.parent / "regions" / state["region"]
        summary = status(state, moment(state["ends_at"]).timestamp())
        with lock(directory / ".lock"):
            atomic_json(directory / "state.json", state)
            atomic_json(directory / "summary.json", summary)
        summaries[network].append(summary)
    return summaries


def merge_network(root: Path, network: str) -> list[dict]:
    """Merge one network (see `merge_networks`); returns its regional summaries."""
    return merge_networks(root, [network])[network]
