"""`campaign shot`: one fixed step per network and region, every worker started at one time.

Runs on the manager like `campaign probe` and reuses its pieces: the worker setup, the step
command and its result, the adopt and the guarded release. One worker per network and region
(`pic-<sid>-h00-<region>-<network>`, every worker marker, its own shot id) is created, confirmed
in project Proxy Index (fail closed) and set up. The start time is recorded before anything is
created; every worker that is ready by then starts its single step at that time, at its
network's concurrency (the plan's cap). A worker not ready by then is reported `late` and never
started. Every worker is removed through `scope.guarded_delete` whatever happens. The results
hold counts, rates and latencies only, never an address.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..onboarding.files import atomic_bytes, atomic_json, private_dir
from ..onboarding.manifest import timestamp
from ..onboarding.scheduler import Clock
from . import ops_api, scope
from .contracts import (
    SIZE_RE,
    WORKER_TAG,
    CampaignSpec,
    Droplet,
    DropletRecord,
    Scope,
    audit_path,
    campaign_tag,
    droplet_name,
    hour_tag,
    short_id,
    spec_path,
)
from .do_api import CreateError, DigitalOcean
from .fanout import READY_POLL_SECONDS, RemoteError
from .launch import CREATE_RETRY_SECONDS, WorkerInputs
from .planspec import network_concurrency
from .probe import (
    REQUESTS,
    SIZE,
    STEP_SECONDS,
    ProbeError,
    ProbeRemote,
    network_problems,
    payload,
    reasons,
    recover_worker,
    release,
    run_command,
    set_up,
    step_result,
)

# The country each region's workers ask for, unless `country_by_network` names one.
REGION_COUNTRY = {"lon1": "GB", "fra1": "DE", "ams3": "NL", "nyc3": "US", "tor1": "CA",
                  "sgp1": "SG", "blr1": "IN", "syd1": "AU"}
LEAD_SECONDS = 600.0  # from the shot's start to the common start time
CREATE_BATCH = 10  # names per create call (DigitalOcean's limit)
SETUP_FLOOR_SECONDS = 30.0


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


def cell_key(network: str, region: str) -> str:
    return f"{network}/{region}"


def markdown(result: dict[str, Any]) -> str:
    """The owner's table: one row per network and region."""
    lines = [f"# Shot {result['shot_id']}", "",
             f"{result['requests']} requests per worker, one step, started at "
             f"{result['start_at']}.", "",
             "| Network | Region | Country | Size | Concurrency | Outcome | Completed | Success % "
             "| 429 % | 5xx % | Connect % | Timeout % | Other % | RPS | p50 ms | p95 ms "
             "| Unique IPs | Start lag s | Step down |",
             "|" + "---|" * 19]
    for key, cell in result["cells"].items():
        step = cell.get("step") or {}
        cells = [*key.split("/"), cell["country"], cell["size"], cell["concurrency"],
                 cell["outcome"], *(step.get(k, "-") for k in (
                     "completed", "success_pct", "rate_limited_pct", "gateway_5xx_pct",
                     "proxy_connect_pct", "timeout_pct", "other_pct", "rps", "latency_p50_ms",
                     "latency_p95_ms", "unique_ips")),
                 cell.get("start_lag_seconds", "-"), ", ".join(cell.get("step_down", [])) or "-"]
        lines.append("| " + " | ".join(map(str, cells)) + " |")
    return "\n".join(lines) + "\n"


class Shot:
    """One shot: its workers, cells and files under `<output>/<shot id>/`."""

    def __init__(self, cloud: DigitalOcean, remotes: Callable[[Path], ProbeRemote],
                 inputs: ShotInputs, clock: Clock):
        self.cloud, self.inputs, self.clock = cloud, inputs, clock
        try:
            self.spec = CampaignSpec.from_json(spec_path(inputs.root).read_text())
            self.files = WorkerInputs.for_root(inputs.root, self.spec.wheel_sha256)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ProbeError(f"no planned campaign with its wheel at --root "
                             f"({type(error).__name__})") from None
        self.networks = inputs.networks or self.spec.networks
        self.regions = inputs.regions or self.spec.regions
        self.check()
        now = datetime.fromtimestamp(clock.now(), UTC)
        self.shot_id = f"{now:%Y%m%dt%H%M%S}z-{secrets.token_hex(6)}"
        self.scope = Scope(self.shot_id, short_id(self.shot_id), self.spec.project_id,
                           self.spec.manager_droplet_id, now - ops_api.CLOCK_SKEW)
        self.tag, self.start_at = hour_tag(self.shot_id, 0), clock.now() + inputs.lead_seconds
        self.folder = inputs.output / self.shot_id
        self.remote = remotes(self.folder)  # its known-hosts file lives in this shot's folder
        self.workers = [DropletRecord(
            network, region, 0, droplet_name(self.shot_id, 0, region, network),
            size=inputs.size_for(network), image_id=self.spec.image_by_region[region],
            concurrency=network_concurrency(network, self.spec.concurrency))
            for region in self.regions for network in self.networks]
        self.result: dict[str, Any] = {
            "shot_id": self.shot_id, "campaign_id": self.spec.campaign_id,
            # What `campaign shot --remove` needs to rebuild this scope in a later process.
            "scope": {"project_id": self.scope.project_id,
                      "manager_droplet_id": self.scope.manager_droplet_id,
                      "created_at": self.scope.created_at.isoformat()},
            "requests": inputs.requests, "start_at": timestamp(self.start_at),
            "cells": {cell_key(w.network, w.region): {
                "country": inputs.country(w.network, w.region), "size": w.size,
                "concurrency": w.concurrency, "outcome": "pending"} for w in self.workers}}

    def check(self) -> None:
        """Every input before anything is created."""
        i = self.inputs
        unknown = set(self.regions) - set(self.spec.regions)
        sizes = {i.size_for(n) for n in self.networks}
        problems = [(bool(unknown), "regions not planned"),
                    (not set(self.networks) <= set(self.spec.networks), "networks not planned"),
                    (any(r not in REGION_COUNTRY for r in self.regions), "region without a "
                     "country"),
                    (not set(i.size_by_network) | set(i.country_by_network) <= set(
                        self.networks), "a per-network setting names an unplanned network"),
                    (any(not re.fullmatch(SIZE_RE, s) for s in sizes), "size"),
                    (i.requests < 1 or i.lead_seconds < 0, "requests or lead")]
        bad = [text for failed, text in problems if failed]
        if not bad:
            for network in self.networks:
                bad += network_problems(i.root, self.spec, self.files, network, [
                    i.country(network, r) for r in self.regions])
        if bad:
            raise ProbeError(f"refused: {', '.join(bad)}")

    def now(self) -> str:
        return timestamp(self.clock.now())

    def save(self) -> None:
        """The record, with each worker's name before its create and its id (never its
        address) right after."""
        self.result["workers"] = [asdict(w) for w in self.workers]
        private_dir(self.folder)
        atomic_json(self.folder / "shot-result.json", self.result)

    def write(self) -> None:
        self.save()
        atomic_bytes(self.folder / "shot-result.md", markdown(self.result).encode())

    async def run(self) -> dict[str, Any]:
        """Create, confirm, set up and fire every worker; every worker is removed whatever
        happens, also when a signal cancels the shot. Workers left behind are reported with
        the recovery command."""
        try:
            await self.check_sizes()
            await self.create_all()
            droplets = await self.active()
            await asyncio.gather(*(self.fire(w, droplets.get(w.droplet_id))
                                   for w in self.workers if w.status == "assigned"))
        except asyncio.CancelledError:
            self.result["error"] = "cancelled by a signal"
        except Exception as error:
            message = scope.SECRETS.sub("[removed]", str(error))
            self.result["error"] = f"{type(error).__name__}: {message}"[:scope.MESSAGE_LIMIT]
        finally:
            cleanup = asyncio.ensure_future(self.cleanup())
            while not cleanup.done():  # a signal during the removal never interrupts it
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    self.result["error"] = "cancelled by a signal"
        return self.result

    async def check_sizes(self) -> None:
        offered = await self.cloud.sizes()
        missing = sorted({f"{w.size} in {w.region}" for w in self.workers
                          if w.region not in offered.get(w.size, [])})
        if missing:
            raise ProbeError(f"not available: {', '.join(missing)}")

    async def create_all(self) -> None:
        """Batches per region and size; then one confirmation of every created worker."""
        groups: dict[tuple[str, str], list[DropletRecord]] = {}
        for worker in self.workers:
            groups.setdefault((worker.region, worker.size), []).append(worker)
        batches = [group[i:i + CREATE_BATCH] for group in groups.values()
                   for i in range(0, len(group), CREATE_BATCH)]
        await asyncio.gather(*(self.create(batch) for batch in batches))
        self.save()
        records = {w.droplet_id: w for w in self.workers if w.droplet_id}
        await scope.assign_and_verify_project(
            self.cloud, self.scope, list(records), records, audit_path(self.folder),
            retry=scope.Retry(sleep=self.clock.sleep, save=self.save))
        self.save()

    async def create(self, batch: list[DropletRecord]) -> None:
        """Names before the create, ids right after; a lost answer is adopted by exact name,
        once more after `CREATE_RETRY_SECONDS` (as a launch does). Never created twice."""
        for worker in batch:
            worker.attempts, worker.requested_at = 1, self.now()
            worker.error_class = "create_uncertain"  # until an id is known or it surely failed
        self.save()
        uncertain = False
        try:
            found = await self.cloud.create(
                [w.name for w in batch], region=batch[0].region, size=batch[0].size,
                image=batch[0].image_id, tags=[WORKER_TAG, campaign_tag(self.shot_id), self.tag],
                ssh_keys=[self.spec.ssh_key_id], monitoring=False, with_droplet_agent=False,
                ipv6=False)
        except CreateError as error:
            found, uncertain = list(error.created), error.uncertain
            for wait in (0.0, CREATE_RETRY_SECONDS) if uncertain else ():
                if not (missing := self.missing(batch, found)):
                    break
                await self.clock.sleep(wait)
                found += await self.adopted(missing)
        by_name = {d.name: d for d in found}
        for worker in batch:
            if worker.name in by_name:
                worker.droplet_id, worker.error_class = by_name[worker.name].id, None
            else:
                worker.status = "create_failed"
                worker.error_class = "create_uncertain" if uncertain else "create_failed"
        self.save()

    @staticmethod
    def missing(batch: list[DropletRecord], found: list[Droplet]) -> list[str]:
        names = {d.name for d in found}
        return [w.name for w in batch if w.name not in names]

    async def adopted(self, names: list[str]) -> list[Droplet]:
        try:
            return await self.cloud.adopt_uncertain(names, self.tag)
        except Exception:
            return []

    async def active(self) -> dict[str, Droplet]:
        """Every confirmed worker once active with an address: one listing by the shot's tag
        per poll, until all are found or the start time passes."""
        waiting = {w.droplet_id: w for w in self.workers if w.status == "assigned"}
        found: dict[str, Droplet] = {}
        while True:
            for droplet in await self.cloud.list_tag(self.tag):
                worker = waiting.get(droplet.id)
                if worker and droplet.status == "active" and droplet.public_ipv4:
                    worker.active_at = worker.active_at or self.now()
                    found[droplet.id] = droplet
            if len(found) == len(waiting) or self.clock.now() >= self.start_at:
                return found
            await self.clock.sleep(READY_POLL_SECONDS)

    async def fire(self, worker: DropletRecord, droplet: Droplet | None) -> None:
        """Ready and set up one worker, then its single step at the common start time; one
        not ready by then is `late` and never started. Any error stays with this worker."""
        cell = self.result["cells"][cell_key(worker.network, worker.region)]
        try:
            ready = droplet is not None and await self.remote.wait_ready(
                droplet, max(self.start_at - self.clock.now(), 1.0))
            if ready:
                worker.ssh_ready_at = self.now()
                await set_up(self.remote, droplet, payload(
                    self.inputs.root, self.spec, self.files, [worker.network]),
                    max(self.start_at - self.clock.now(), SETUP_FLOOR_SECONDS))
                worker.status, worker.armed_at = "armed", self.now()
        except (RemoteError, ProbeError) as error:
            worker.status, worker.error_class = "start_failed", type(error).__name__
            cell["outcome"] = "setup_failed"
            return
        if worker.status != "armed" or self.clock.now() > self.start_at:
            cell["outcome"] = "late"  # reported, never started late
            return
        await self.clock.sleep(self.start_at - self.clock.now())
        worker.status, worker.started_at = "running", self.now()
        cell["start_lag_seconds"] = round(self.clock.now() - self.start_at, 3)
        cell["step"] = step = await self.step(worker, droplet, cell["country"])
        cell["step_down"] = reasons(step, step)  # the absolute rules; the probe compares later
        cell["outcome"] = "run_failed" if step.get("run_failed") else "measured"
        worker.status, worker.finished_at = "finished", self.now()
        self.write()

    async def step(self, worker: DropletRecord, droplet: Droplet, country: str) -> dict:
        command = run_command(worker.network, worker.concurrency, self.inputs.requests,
                              country, worker.region)
        try:
            reply = await self.remote.run(droplet, command, f"shot {worker.network}",
                                          seconds=STEP_SECONDS)
            summary = json.loads(reply.decode().strip().splitlines()[-1])
            return step_result(worker.concurrency, self.inputs.requests, summary)
        except (RemoteError, ValueError, IndexError, KeyError, TypeError) as error:
            return {"concurrency": worker.concurrency, "requested": self.inputs.requests,
                    "run_failed": type(error).__name__}

    async def cleanup(self) -> None:
        """Remove every created worker through the guard; report what is left."""
        for worker in self.workers:
            if worker.droplet_id is None and worker.error_class == "create_uncertain":
                found = await self.adopted([worker.name])  # one last look before reporting
                if found:
                    worker.droplet_id, worker.error_class = found[0].id, None
        await asyncio.gather(*(release(self.cloud, self.scope, w, audit_path(self.folder),
                                       self.clock.sleep)
                               for w in self.workers if w.droplet_id is not None))
        left = [w.name for w in self.workers if w.error_class == "create_uncertain" or (
            w.droplet_id is not None and w.status != "destroyed")]
        refused = [w.name for w in self.workers if w.status == "destroy_refused"]
        if refused:  # the owner's to decide: never retried by --remove
            self.result["destroy_refused"] = refused
        self.result["workers_left"] = len(left)
        if left:
            self.result["recover"] = f"campaign shot --remove {self.folder}"
        self.write()


async def recover(cloud: DigitalOcean, folder: Path, *,
                  sleep: ops_api.Sleep = asyncio.sleep) -> dict[str, Any]:
    """`campaign shot --remove <folder>`: remove the workers a stopped shot left, each by its
    exact name under the shot's hour tag (and recorded id), through `probe.recover_worker`.
    Writes the outcome back into the record; never touches anything the record does not name."""
    path = folder / "shot-result.json"
    try:
        result = json.loads(path.read_text())
        recorded = result["scope"]
        workers = [DropletRecord(**raw) for raw in result["workers"]]
        shot_scope = Scope(result["shot_id"], short_id(result["shot_id"]),
                           recorded["project_id"], recorded["manager_droplet_id"],
                           datetime.fromisoformat(recorded["created_at"]))
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ProbeError(f"no shot record in that folder ({type(error).__name__})") from None
    try:
        for worker in workers:
            await recover_worker(cloud, shot_scope, worker, audit_path(folder), sleep)
    finally:  # what was removed stays recorded, also when a later read fails
        left = sum(w.status not in ("destroyed", "absent") for w in workers)
        result.update(workers=[asdict(w) for w in workers], workers_left=left,
                      recovered_at=timestamp(datetime.now(UTC).timestamp()))
        if not left:
            result.pop("recover", None)
        atomic_json(path, result)
    return {"shot_id": result["shot_id"], "workers_left": left}


async def shot(cloud: DigitalOcean, remotes: Callable[[Path], ProbeRemote], inputs: ShotInputs,
               *, clock: Clock | None = None) -> dict[str, Any]:
    """Run one shot; returns the result (also in `<output>/<shot id>/shot-result.*`)."""
    return await Shot(cloud, remotes, inputs, clock or Clock()).run()
