"""The step plan and result helpers of `campaign shot`; `shot.py` imports them back.

The plan: the shot's inputs, every one checked before anything is created, and one worker and
one cell per network and region. The result: each cell's single step and the owner's table.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..onboarding.manifest import timestamp
from ..providers.grammar import load_grammar
from .contracts import (
    SIZE_RE,
    CampaignSpec,
    Droplet,
    DropletRecord,
    Scope,
    droplet_name,
    spec_path,
)
from .fanout import RemoteError
from .launch import WorkerInputs
from .planspec import network_concurrency
from .probe import (
    CLASS_HEADER,
    REQUESTS,
    SIZE,
    STEP_SECONDS,
    ProbeError,
    ProbeRemote,
    class_cells,
    network_problems,
    run_command,
    step_result,
)

# The country each region's workers ask for, unless `country_by_network` names one.
REGION_COUNTRY = {
    "lon1": "GB",
    "fra1": "DE",
    "ams3": "NL",
    "nyc3": "US",
    "tor1": "CA",
    "sgp1": "SG",
    "blr1": "IN",
    "syd1": "AU",
}
LEAD_SECONDS = 600.0  # from the shot's start to the common start time


@dataclass(frozen=True)
class ShotInputs:
    root: Path  # a planned campaign uploaded to this manager: its spec and network configs
    output: Path  # the shot writes `<output>/<shot id>/`
    networks: tuple[str, ...] = ()  # default: the campaign's networks
    regions: tuple[str, ...] = ()  # default: the campaign's regions
    requests: int = REQUESTS
    size: str = SIZE
    size_by_network: Mapping[str, str] = field(default_factory=dict)
    country_by_network: Mapping[str, str] = field(default_factory=dict)
    lead_seconds: float = LEAD_SECONDS

    def country(self, network: str, region: str) -> str:
        return self.country_by_network.get(network) or REGION_COUNTRY[region]

    def size_for(self, network: str) -> str:
        return self.size_by_network.get(network, self.size)


def list_country(network: str) -> bool:
    """Whether the network has no free country choice: no country flag, or a preset one (only
    its dashboard list's country loads), so its cells need `country_by_network`."""
    flag = load_grammar(network).flags.get("country")
    return flag is None or flag.preset


def pending_outcome(worker: DropletRecord) -> str:
    """The outcome of a cell that never ran, from its worker's status before the removal."""
    if worker.assign_failed_at:
        return "unconfirmed"
    if worker.status == "create_failed":
        return "create_failed"
    if worker.droplet_id is None and worker.error_class is None:
        return "not_created"
    return "cancelled"  # stopped mid-way: by a signal or the shot's own error


def cell_key(network: str, region: str) -> str:
    return f"{network}/{region}"


def markdown(result: dict[str, Any]) -> str:
    """The owner's table: one row per network and region."""
    lines = [
        f"# Shot {result['shot_id']}",
        "",
        f"{result['requests']} requests per worker, one step, started at {result['start_at']}.",
        "",
        "| Network | Region | Country | Size | Concurrency | Outcome | Completed | Success % "
        "| Rate-limited % | 5xx % | Connect % | Timeout % | Other % | RPS | p50 ms | p95 ms "
        f"| Unique IPs | {CLASS_HEADER} | Start lag s | Step down |",
        "|" + "---|" * 23,
    ]
    for key, cell in result["cells"].items():
        step = cell.get("step") or {}
        cells = [
            *key.split("/"),
            cell["country"],
            cell["size"],
            cell["concurrency"],
            cell["outcome"],
            *(
                step.get(k, "-")
                for k in (
                    "completed",
                    "success_pct",
                    "rate_limited_pct",
                    "gateway_5xx_pct",
                    "proxy_connect_pct",
                    "timeout_pct",
                    "other_pct",
                    "rps",
                    "latency_p50_ms",
                    "latency_p95_ms",
                    "unique_ips",
                )
            ),
            *class_cells(step),
            cell.get("start_lag_seconds", "-"),
            ", ".join(cell.get("step_down", [])) or "-",
        ]
        lines.append("| " + " | ".join(map(str, cells)) + " |")
    return "\n".join(lines) + "\n"


class ShotSteps:
    """The plan and the step of one `shot.Shot`: its checked inputs, its workers and cells, and
    its single step's result. `Shot` sets the attributes below before calling any of these."""

    inputs: ShotInputs
    shot_id: str
    scope: Scope
    start_at: float
    folder: Path
    remote: ProbeRemote
    workers: list[DropletRecord]

    def load(self) -> None:
        """The planned spec and its worker files, and the networks and regions, all checked."""
        inputs = self.inputs
        try:
            self.spec = CampaignSpec.from_json(spec_path(inputs.root).read_text())
            self.files = WorkerInputs.for_root(inputs.root, self.spec.wheel_sha256)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ProbeError(
                f"no planned campaign with its wheel at --root ({type(error).__name__})"
            ) from None
        self.networks = inputs.networks or self.spec.networks
        self.regions = inputs.regions or self.spec.regions
        self.check()

    def worker_records(self) -> list[DropletRecord]:
        """One worker per region and network, at its network's size and concurrency."""
        return [
            DropletRecord(
                network,
                region,
                0,
                droplet_name(self.shot_id, 0, region, network),
                size=self.inputs.size_for(network),
                image_id=self.spec.image_by_region[region],
                concurrency=network_concurrency(network, self.spec.concurrency),
            )
            for region in self.regions
            for network in self.networks
        ]

    def start_result(self) -> dict[str, Any]:
        """The result before any worker is created: every cell `pending`."""
        inputs = self.inputs
        return {
            "shot_id": self.shot_id,
            "campaign_id": self.spec.campaign_id,
            # What `campaign shot --remove` needs to rebuild this scope in a later process.
            "scope": {
                "project_id": self.scope.project_id,
                "manager_droplet_id": self.scope.manager_droplet_id,
                "created_at": self.scope.created_at.isoformat(),
            },
            "requests": inputs.requests,
            "start_at": timestamp(self.start_at),
            # Until the removal reports no worker left (a killed or crashed shot keeps it).
            "recover": f"campaign shot --remove {self.folder}",
            "cells": {
                cell_key(w.network, w.region): {
                    "country": inputs.country(w.network, w.region),
                    "size": w.size,
                    "concurrency": w.concurrency,
                    "outcome": "pending",
                }
                for w in self.workers
            },
        }

    def check(self) -> None:
        """Every input before anything is created."""
        i = self.inputs
        unknown = set(self.regions) - set(self.spec.regions)
        sizes = {i.size_for(n) for n in self.networks}
        repeated = len(set(self.regions)) != len(self.regions) or len(set(self.networks)) != len(
            self.networks
        )
        problems = [
            (repeated, "regions or networks repeated"),
            (bool(unknown), "regions not planned"),
            (not set(self.networks) <= set(self.spec.networks), "networks not planned"),
            (any(r not in REGION_COUNTRY for r in self.regions), "region without a country"),
            (
                not set(i.size_by_network) | set(i.country_by_network) <= set(self.networks),
                "a per-network setting names an unplanned network",
            ),
            (any(not re.fullmatch(SIZE_RE, s) for s in sizes), "size"),
            (i.requests < 1 or i.lead_seconds < 0, "requests or lead"),
        ]
        bad = [text for failed, text in problems if failed]
        bad += [
            f"{n} only loads its list's country: give --country-by-network {n}=<it>"
            for n in self.networks
            if list_country(n) and n not in i.country_by_network
        ]
        if not bad:
            for network in self.networks:
                bad += network_problems(
                    i.root,
                    self.spec,
                    self.files,
                    network,
                    [i.country(network, r) for r in self.regions],
                )
        if bad:
            raise ProbeError(f"refused: {', '.join(bad)}")

    async def step(self, worker: DropletRecord, droplet: Droplet, country: str) -> dict:
        command = run_command(
            worker.network, worker.concurrency, self.inputs.requests, country, worker.region
        )
        try:
            reply = await self.remote.run(
                droplet, command, f"shot {worker.network}", seconds=STEP_SECONDS
            )
            summary = json.loads(reply.decode().strip().splitlines()[-1])
            return step_result(worker.concurrency, self.inputs.requests, summary)
        except (RemoteError, ValueError, IndexError, KeyError, TypeError) as error:
            return {
                "concurrency": worker.concurrency,
                "requested": self.inputs.requests,
                "run_failed": type(error).__name__,
            }
