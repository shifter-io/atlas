"""`campaign shot`: one fixed step per network and region, every worker started at one time.

Runs on the manager like `campaign probe` and reuses its pieces: the worker setup, the step
command and its result, the adopt and the guarded release. One worker per network and region
(`atlas-<sid>-h00-<region>-<network>`, every worker marker, its own shot id) is created, confirmed
in project Atlas (fail closed) and set up. The start time is recorded before anything is
created; every worker that is ready by then starts its single step at that time, at its
network's concurrency (the plan's cap). A worker not ready by then is reported `late` and never
started. Every worker is removed through `scope.guarded_delete` whatever happens. The results
hold counts, rates and latencies only, never an address.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..onboarding.files import atomic_bytes, atomic_json, private_dir
from ..onboarding.manifest import timestamp
from ..onboarding.scheduler import Clock
from . import ops_api, scope
from .contracts import (
    WORKER_TAG,
    Droplet,
    DropletRecord,
    Scope,
    audit_path,
    campaign_tag,
    hour_tag,
    short_id,
)
from .do_api import CreateError, DigitalOcean
from .fanout import READY_POLL_SECONDS, RemoteError
from .launch import CREATE_RETRY_SECONDS
from .probe import (  # noqa: F401 (REQUESTS and SIZE: the CLI's defaults)
    REQUESTS,
    SIZE,
    ProbeError,
    ProbeRemote,
    payload,
    reasons,
    release,
    set_up,
)
from .recover import recover_worker
from .shot_steps import (  # noqa: F401 (callers keep importing these from here)
    LEAD_SECONDS,
    REGION_COUNTRY,
    ShotInputs,
    ShotSteps,
    cell_key,
    list_country,
    markdown,
    pending_outcome,
)

CREATE_BATCH = 10  # names per create call (DigitalOcean's limit)


class Shot(ShotSteps):
    """One shot: its workers, cells and files under `<output>/<shot id>/`."""

    def __init__(
        self,
        cloud: DigitalOcean,
        remotes: Callable[[Path], ProbeRemote],
        inputs: ShotInputs,
        clock: Clock,
    ):
        self.cloud, self.inputs, self.clock = cloud, inputs, clock
        self.load()
        now = datetime.fromtimestamp(clock.now(), UTC)
        self.shot_id = f"{now:%Y%m%dt%H%M%S}z-{secrets.token_hex(6)}"
        self.scope = Scope(
            self.shot_id,
            short_id(self.shot_id),
            self.spec.project_id,
            self.spec.manager_droplet_id,
            now - ops_api.CLOCK_SKEW,
        )
        self.tag, self.start_at = hour_tag(self.shot_id, 0), clock.now() + inputs.lead_seconds
        self.folder = inputs.output / self.shot_id
        self.remote = remotes(self.folder)  # its known-hosts file lives in this shot's folder
        self.workers = self.worker_records()
        if len({w.name for w in self.workers}) != len(self.workers):  # one record per name
            raise ProbeError("refused: worker names repeated")
        # A droplet a create or adopt returned that no worker record holds (a second one with
        # a worker's name): recorded, removed through the guard and counted in workers_left.
        self.unrecorded: list[DropletRecord] = []
        self.result: dict[str, Any] = self.start_result()

    def now(self) -> str:
        return timestamp(self.clock.now())

    def save(self) -> None:
        """The record, with each worker's name before its create and its id (never its
        address) right after."""
        self.result["workers"] = [asdict(w) for w in self.workers]
        self.result["unrecorded"] = [asdict(w) for w in self.unrecorded]
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
            await asyncio.gather(
                *(
                    self.fire(w, droplets.get(w.droplet_id))
                    for w in self.workers
                    if w.status == "assigned"
                )
            )
        except asyncio.CancelledError:
            self.result["error"] = "cancelled by a signal"
        except Exception as error:  # boundary: the shot always reports; the cleanup still runs
            message = scope.SECRETS.sub("[removed]", str(error))
            self.result["error"] = f"{type(error).__name__}: {message}"[: scope.MESSAGE_LIMIT]
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
        missing = sorted(
            {
                f"{w.size} in {w.region}"
                for w in self.workers
                if w.region not in offered.get(w.size, [])
            }
        )
        if missing:
            raise ProbeError(f"not available: {', '.join(missing)}")

    async def create_all(self) -> None:
        """Batches per region and size; then one confirmation of every created worker."""
        groups: dict[tuple[str, str], list[DropletRecord]] = {}
        for worker in self.workers:
            groups.setdefault((worker.region, worker.size), []).append(worker)
        batches = [
            group[i : i + CREATE_BATCH]
            for group in groups.values()
            for i in range(0, len(group), CREATE_BATCH)
        ]
        async with asyncio.TaskGroup() as group:  # an error cancels the other creates
            for batch in batches:
                group.create_task(self.create(batch))
        self.save()
        records = {w.droplet_id: w for w in self.workers + self.unrecorded if w.droplet_id}
        await scope.assign_and_verify_project(
            self.cloud,
            self.scope,
            list(records),
            records,
            audit_path(self.folder),
            retry=scope.Retry(sleep=self.clock.sleep, save=self.save),
        )
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
                [w.name for w in batch],
                region=batch[0].region,
                size=batch[0].size,
                image=batch[0].image_id,
                tags=[WORKER_TAG, campaign_tag(self.shot_id), self.tag],
                ssh_keys=[self.spec.ssh_key_id],
                monitoring=False,
                with_droplet_agent=False,
                ipv6=False,
            )
        except CreateError as error:
            found, uncertain = list(error.created), error.uncertain
            for wait in (0.0, CREATE_RETRY_SECONDS) if uncertain else ():
                if not (missing := self.missing(batch, found)):
                    break
                await self.clock.sleep(wait)
                found += await self.adopted(missing)
        self.take(batch, found, "create_uncertain" if uncertain else "create_failed")
        self.save()

    def take(self, workers: list[DropletRecord], found: list[Droplet], failed: str | None) -> None:
        """Give each worker the first droplet with its name; every other droplet found is
        recorded in `unrecorded` (never dropped). A worker with none gets `failed`."""
        by_name: dict[str, list[Droplet]] = {}
        for droplet in found:
            if droplet.id not in {w.droplet_id for w in self.workers + self.unrecorded}:
                by_name.setdefault(droplet.name, []).append(droplet)
        for worker in workers:
            if mine := by_name.get(worker.name):
                worker.droplet_id, worker.error_class = mine.pop(0).id, None
            elif failed:
                worker.status, worker.error_class = "create_failed", failed
        for droplet in (d for extra in by_name.values() for d in extra):
            region, network = droplet.name.split("-h00-", 1)[1].split("-", 1)
            self.unrecorded.append(
                DropletRecord(
                    network,
                    region,
                    0,
                    droplet.name,
                    droplet_id=droplet.id,
                    error_class="unrecorded",
                )
            )

    @staticmethod
    def missing(batch: list[DropletRecord], found: list[Droplet]) -> list[str]:
        names = {d.name for d in found}
        return [w.name for w in batch if w.name not in names]

    async def adopted(self, names: list[str]) -> list[Droplet]:
        try:
            return await self.cloud.adopt_uncertain(names, self.tag)
        except Exception as error:  # boundary: also the cleanup's last look, which must go on
            self.result["adopt_error"] = type(error).__name__
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
            await self.shoot(worker, droplet, cell)
        except Exception as error:  # boundary: never a sibling's problem; a cancel propagates
            worker.error_class = type(error).__name__
            cell["outcome"] = "failed" if worker.started_at else "setup_failed"

    async def shoot(
        self, worker: DropletRecord, droplet: Droplet | None, cell: dict[str, Any]
    ) -> None:
        try:
            ready = droplet is not None and await self.remote.wait_ready(
                droplet, max(self.start_at - self.clock.now(), 1.0)
            )
            left = self.start_at - self.clock.now()
            if ready and left > 0:  # credentials only go to a worker that can still start
                worker.ssh_ready_at = self.now()
                await set_up(
                    self.remote,
                    droplet,
                    payload(self.inputs.root, self.spec, self.files, [worker.network]),
                    left,
                )
                worker.status, worker.armed_at = "armed", self.now()
        except (RemoteError, ProbeError) as error:
            worker.status, worker.error_class = "start_failed", type(error).__name__
            cell["outcome"] = "late" if self.clock.now() >= self.start_at else "setup_failed"
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

    async def cleanup(self) -> None:
        """Remove every created worker through the guard; report what is left."""
        uncertain = [
            w for w in self.workers if w.droplet_id is None and w.error_class == "create_uncertain"
        ]
        if uncertain:  # one last look before reporting them
            self.take(uncertain, await self.adopted([w.name for w in uncertain]), None)
        for worker in self.workers:
            cell = self.result["cells"][cell_key(worker.network, worker.region)]
            if cell["outcome"] == "pending":
                cell["outcome"] = pending_outcome(worker)
        everyone = self.workers + self.unrecorded
        await asyncio.gather(
            *(
                release(self.cloud, self.scope, w, audit_path(self.folder), self.clock.sleep)
                for w in everyone
                if w.droplet_id is not None
            )
        )
        left = [
            w.name
            for w in everyone
            if w.error_class == "create_uncertain"
            or (w.droplet_id is not None and w.status != "destroyed")
        ]
        refused = [w.name for w in everyone if w.status == "destroy_refused"]
        if refused:  # the owner's to decide: never retried by --remove
            self.result["destroy_refused"] = refused
        self.result["workers_left"] = len(left)
        if not left:
            self.result.pop("recover", None)
        self.write()


async def recover(
    cloud: DigitalOcean, folder: Path, *, sleep: ops_api.Sleep = asyncio.sleep
) -> dict[str, Any]:
    """`campaign shot --remove <folder>`: remove the workers a stopped shot left, each by its
    exact name under the shot's hour tag (and recorded id), through `recover.recover_worker`.
    Writes the outcome back into the record; never touches anything the record does not name."""
    path = folder / "shot-result.json"
    try:
        result = json.loads(path.read_text())
        recorded = result["scope"]
        workers = [DropletRecord(**raw) for raw in result["workers"] + result.get("unrecorded", [])]
        shot_scope = Scope(
            result["shot_id"],
            short_id(result["shot_id"]),
            recorded["project_id"],
            recorded["manager_droplet_id"],
            datetime.fromisoformat(recorded["created_at"]),
        )
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ProbeError(f"no shot record in that folder ({type(error).__name__})") from None
    try:
        for worker in list(workers):  # a second droplet with a name joins `unrecorded`
            workers += await recover_worker(cloud, shot_scope, worker, audit_path(folder), sleep)
    finally:  # what was removed stays recorded, also when a later read fails
        left = sum(w.status not in ("destroyed", "absent") for w in workers)
        count = len(result["workers"])
        result.update(
            workers=[asdict(w) for w in workers[:count]],
            unrecorded=[asdict(w) for w in workers[count:]],
            workers_left=left,
            recovered_at=timestamp(datetime.now(UTC).timestamp()),
        )
        if not left:
            result.pop("recover", None)
        atomic_json(path, result)
    return {"shot_id": result["shot_id"], "workers_left": left}


async def shot(
    cloud: DigitalOcean,
    remotes: Callable[[Path], ProbeRemote],
    inputs: ShotInputs,
    *,
    clock: Clock | None = None,
) -> dict[str, Any]:
    """Run one shot; returns the result (also in `<output>/<shot id>/shot-result.*`)."""
    return await Shot(cloud, remotes, inputs, clock or Clock()).run()
