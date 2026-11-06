"""Launch one hour: create, confirm project membership, wait for readiness, push and arm.

Names are persisted before the create and ids right after it. Only workers whose membership in
project Proxy Index is confirmed are ever pushed (fail-closed). A create, assignment, boot, push
or start failure gets a replacement in the same hour, up to `REPLACEMENTS_PER_CELL` per cell and
only before `replace_until_minutes` (at most the slot length) past the slot's start or past a
later launch; then the cell is `failed_ours` with a high alert. Every removal goes through
`scope.guarded_delete`, that of a worker whose membership was never confirmed on the
own-unassigned path. A resumed launch and
every replacement adopt their own names before they create anything, so nothing is created
twice.
"""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..onboarding.files import atomic_json, digest, lock, read_json
from ..onboarding.manifest import moment, region_countries, timestamp
from ..onboarding.scheduler import Clock
from . import scope
from .contracts import (
    CampaignSpec,
    Droplet,
    DropletRecord,
    HourRecord,
    Remote,
    audit_path,
    campaign_tag,
    droplet_name,
    hour_path,
    hour_tag,
    runtime_dir,
    tags,
)
from .do_api import CreateError, DigitalOcean
from .fanout import READY_POLL_SECONDS, start_command

READY_LIMIT_SECONDS = 180
CREATE_RETRY_SECONDS = 15
SYNTHETIC_MINUTES = 5
REPLACEMENTS_PER_CELL = 3
LIVE = frozenset({"armed", "running"})
UP = ("requested", "assigned", "booting", "starting")
RemoteFactory = Callable[[int], Remote]
TRAFFIC_KEYS = (
    "bytes_up",
    "bytes_down",
    "requests",
    "successes",
    "errors",
    "countries_done",
    "countries_total",
    "worker_hours",
)


@dataclass(frozen=True)
class WorkerInputs:
    """Manager-side files a worker push reads: the campaign wheel and per-network credentials."""

    wheel: Path
    credentials_dir: Path

    @classmethod
    def for_root(cls, root: Path, wheel_sha256: str) -> WorkerInputs:
        """`<base>/campaigns/<cid>`: the spec's wheel in `<base>/wheels`, `<base>/.secrets`."""
        base = root.parent.parent
        wheels = [w for w in sorted((base / "wheels").glob("*.whl")) if digest(w) == wheel_sha256]
        if not wheels:
            raise ValueError("no manager wheel matches the campaign spec")
        return cls(wheels[0], base / ".secrets" / "credentials")


def cell_key(network: str, region: str) -> str:
    return f"{network}/{region}"


def manifest_path(root: Path, spec: CampaignSpec, network: str) -> Path:
    return root / spec.manifests[network]


def hosts_path(root: Path, hour: int) -> Path:
    return runtime_dir(root) / f"hosts-{hour:02d}.json"


def load_hour(root: Path, hour: int) -> HourRecord | None:
    path = hour_path(root, hour)
    return HourRecord.from_dict(read_json(path)) if path.exists() else None


def read_hosts(root: Path, hour: int) -> dict[str, str]:
    path = hosts_path(root, hour)
    return {str(k): str(v) for k, v in read_json(path).items()} if path.exists() else {}


class HourWork:
    """One pass over one hour's record: saving, alerts and guarded removal."""

    def __init__(
        self,
        spec: CampaignSpec,
        record: HourRecord,
        root: Path,
        cloud: DigitalOcean | None,
        clock: Clock,
    ):
        self.spec, self.record, self.root, self.cloud, self.clock = spec, record, root, cloud, clock

    def now(self) -> str:
        return timestamp(self.clock.now())

    def save(self) -> None:
        atomic_json(hour_path(self.root, self.record.hour), self.record.to_dict())

    def alert(self, level: str, kind: str, name: str | None = None) -> None:
        """One alert per kind and droplet name; alerts hold names, never addresses."""
        if not any(a["kind"] == kind and a.get("name") == name for a in self.record.alerts):
            self.record.alerts.append(
                {"time": self.now(), "level": level, "kind": kind, "name": name}
            )

    async def retire(self, worker: DropletRecord) -> None:
        """Remove one worker through the guard; a refusal is final, an error retried later."""
        workers = [d for d in self.record.droplets if d.droplet_id]
        ledger = scope.Ledger(
            {"droplet": [d.droplet_id for d in workers]}, {d.droplet_id: d for d in workers}
        )
        try:
            await scope.guarded_delete(
                self.cloud,
                "droplet",
                str(worker.droplet_id),
                scope.scope_for(self.spec),
                ledger,
                audit_path(self.root),
            )
        except scope.OwnershipError:
            worker.status = "destroy_refused"
            self.alert("high", "destroy_refused", worker.name)
            return
        except Exception as error:  # audited by the guard; the next pass tries again
            worker.status = "destroy_failed"
            worker.error_class = worker.error_class or type(error).__name__
            return
        worker.status, worker.destroyed_at = "destroyed", self.now()
        self.add_hours(worker)

    def add_hours(self, worker: DropletRecord) -> None:
        start = moment(worker.requested_at or self.record.scheduled_at).timestamp()
        self.record.traffic[worker.network]["worker_hours"] += round(
            max(0.0, self.clock.now() - start) / 3600, 4
        )


async def take_in_flight(spec, record, root, cloud, clock) -> None:
    """For a clean-up: workers whose create was in flight, taken by exact name under the hour
    tag and put in the campaign project (one never confirmed is removed there), so the guard
    can remove them."""
    work = _Launch(spec, record, root, cloud, lambda hour: None, clock)
    await work._adopt()
    await work._assign([w for w in record.droplets if w.droplet_id in cloud.created_ids])


async def launch_hour(
    spec: CampaignSpec,
    record: HourRecord,
    root: Path,
    *,
    cloud: DigitalOcean | None,
    remote_factory: RemoteFactory | None,
    clock: Clock | None = None,
) -> HourRecord:
    """Bring hour `record.hour` from planned (or a crashed launch) to running; idempotent.

    Synthetic specs make no cloud or SSH calls. The record is saved after every step under the
    hour's lock; the launcher owns it until it reaches `running`.
    """
    with lock(hour_path(root, record.hour).parent / ".lock"):
        await _Launch(spec, record, root, cloud, remote_factory, clock or Clock()).run()
    return record


class _Launch(HourWork):
    def __init__(self, spec, record, root, cloud, remote_factory, clock):
        super().__init__(spec, record, root, cloud, clock)
        self.tag = hour_tag(spec.campaign_id, record.hour)
        self.scope = scope.scope_for(spec)
        self.remote = None if spec.synthetic else remote_factory(record.hour)
        self.hosts = read_hosts(root, record.hour)
        self.listed, self.listed_at, self.list_lock = [], float("-inf"), asyncio.Lock()
        self.scheduled = moment(record.scheduled_at).timestamp()

    async def run(self) -> None:
        record = self.record
        if record.state not in ("planned", "launching"):
            return
        record.state, record.fired_at = "launching", record.fired_at or self.now()
        # The window opens at the slot's start, or at a later launch (`tick --now`, a late timer).
        self.replace_until = max(self.scheduled, moment(record.fired_at).timestamp()) + (
            self.spec.replace_until_minutes * 60
        )
        if not record.droplets:
            self._plan_workers()
        self.save()
        if self.spec.synthetic:
            self._synthetic()
        else:
            await self._launch()
        # The newest record per cell decides: a replacement supersedes the worker it replaces.
        record.cells.update(
            {
                cell_key(d.network, d.region): "running" if d.status in LIVE else "failed_ours"
                for d in record.droplets
            }
        )
        record.state, record.launched_at = "running", self.now()
        self.save()

    def _plan_workers(self) -> None:
        spec, record = self.spec, self.record
        earlier = [load_hour(self.root, h) for h in range(record.hour)]
        live = [d for r in earlier if r for d in r.droplets if d.status in LIVE]
        for network in spec.networks:
            manifest = read_json(manifest_path(self.root, spec, network))
            concurrency = manifest["settings"]["concurrency"]
            # The plan caps a network at its grammar's max_concurrency, never above the spec.
            if type(concurrency) is not int or not 1 <= concurrency <= spec.concurrency:
                raise ValueError("a manifest's concurrency exceeds the campaign spec")
            if manifest.get("slot_minutes", 60) != spec.slot_minutes:
                raise ValueError("a manifest's slot_minutes differs from the campaign spec")
            # A daily test's manifest names the countries due; a region with none gets no worker.
            due = {r: len(region_countries(manifest, r)) for r in spec.regions}
            record.traffic[network] = dict.fromkeys(TRAFFIC_KEYS, 0)
            record.traffic[network]["countries_total"] = sum(due.values())
            for region in (r for r in spec.regions if due[r]):
                record.droplets.append(
                    self._worker(
                        network,
                        region,
                        sum(d.network == network and d.region == region for d in live),
                        concurrency,
                    )
                )
                record.cells[cell_key(network, region)] = "pending"

    def _worker(
        self, network: str, region: str, overlap: int, concurrency: int | None
    ) -> DropletRecord:
        return DropletRecord(
            network,
            region,
            self.record.hour,
            droplet_name(self.spec.campaign_id, self.record.hour, region, network),
            size=self.spec.size_by_region[region],
            image_id=self.spec.image_by_region[region],
            overlap=overlap,
            concurrency=concurrency,
        )

    def _synthetic(self) -> None:
        now = self.now()
        for index, worker in enumerate(self.record.droplets):
            if worker.status in UP:
                worker.droplet_id = worker.droplet_id or f"synthetic-{index}"
                worker.requested_at = worker.requested_at or now
                worker.status, worker.armed_at = "armed", now

    async def _launch(self) -> None:
        self.inputs = WorkerInputs.for_root(self.root, self.spec.wheel_sha256)
        await self.clock.sleep(
            max(0.0, self.scheduled - self.spec.create_lead_seconds - self.clock.now())
        )
        await self._adopt()
        await asyncio.gather(
            *(
                self._region(
                    [d for d in self.record.droplets if d.region == region and d.status in UP]
                )
                for region in self.spec.regions
            )
        )

    async def _adopt(self) -> None:
        """A resumed launch: our own recorded names under the hour tag, before any create."""
        waiting = [
            d
            for d in self.record.droplets
            if d.status in UP
            and (d.droplet_id is None or d.droplet_id not in self.cloud.created_ids)
        ]
        if waiting:
            self._take(
                [d for d in waiting if d.droplet_id is None],
                await self._adopted([d.name for d in waiting]),
            )
            self.save()

    async def _adopted(self, names: list[str]) -> list[Droplet]:
        """Our droplets with exactly these names under the hour tag, not held by a record."""
        known = {d.droplet_id for d in self.record.droplets if d.droplet_id}
        return [d for d in await self.cloud.adopt_uncertain(names, self.tag) if d.id not in known]

    def _take(self, workers: list[DropletRecord], droplets: list[Droplet]) -> None:
        by_name = {d.name: d for d in reversed(droplets)}  # the first droplet per name
        for worker in workers:
            if worker.droplet_id is None and worker.name in by_name:
                worker.droplet_id = by_name.pop(worker.name).id

    async def _region(self, workers: list[DropletRecord]) -> None:
        """One region's workers. An unexpected error stays inside this region, no task outlives
        it, and every worker leaves it armed or in a final status (B1)."""
        cause, detail, unassigned = None, None, []
        try:
            await self._create(workers)
            unassigned = await self._assign(workers)
        except Exception as error:
            cause = type(error).__name__
            detail = scope.error_detail(
                self.cloud, [w.droplet_id for w in workers if w.droplet_id], error
            )
        await asyncio.gather(
            *(
                self._bring_up(w)
                for w in workers
                if w.status in ("assigned", "booting", "starting")
            ),
            *(self._replace(w) for w in unassigned),
            *(self._replace(w) for w in workers if w.status == "create_failed"),
        )
        left = [w for w in workers if w.status in UP]
        for worker in left:
            self.alert("high", "launch_error", worker.name)
            worker.error_detail = detail
            if worker.droplet_id is None:
                worker.status, worker.error_class = "create_failed", cause
        await asyncio.gather(
            *(self._fail(w, "start_failed", cause) for w in left if w.droplet_id),
            *(self._replace(w) for w in left if w.droplet_id is None),
        )
        self.save()

    async def _create(self, workers: list[DropletRecord]) -> None:
        pending = [w for w in workers if w.droplet_id is None and w.status == "requested"]
        if any(w.replaced_from is not None for w in pending):  # an earlier create may have landed
            self._take(pending, await self._adopted([w.name for w in pending]))
            for worker in [w for w in pending if w.droplet_id and not w.requested_at]:
                worker.requested_at = timestamp(self.clock.now())  # its boot deadline starts now
            pending = [w for w in pending if w.droplet_id is None]
        for attempt in range(2):
            if attempt and pending:  # a create still in flight may be listed only now (m2)
                await self.clock.sleep(CREATE_RETRY_SECONDS)
                self._take(pending, await self._adopted([w.name for w in pending]))
                pending = [w for w in pending if w.droplet_id is None]
            if not pending:
                return
            for worker in pending:
                worker.attempts, worker.requested_at = (
                    worker.attempts + 1,
                    timestamp(self.clock.now()),
                )
            self.save()
            self._take(pending, await self._create_call(pending))
            self.save()
            pending = [w for w in pending if w.droplet_id is None]
        for worker in pending:
            worker.status, worker.error_class = "create_failed", "create_failed"
            self.alert("high", "create_failed", worker.name)

    async def _create_call(self, workers: list[DropletRecord]) -> list[Droplet]:
        names, region = [w.name for w in workers], workers[0].region
        worker = tags(self.spec.environment)["worker"]  # CAMP-STAGING-1: its environment's
        try:
            return await self.cloud.create(
                names,
                region=region,
                size=self.spec.size_by_region[region],
                image=self.spec.image_by_region[region],
                tags=[worker, campaign_tag(self.spec.campaign_id), self.tag],
                ssh_keys=[self.spec.ssh_key_id],
                monitoring=False,
                with_droplet_agent=False,
                ipv6=False,
            )
        except CreateError as error:
            return error.created + (await self._adopted(error.pending) if error.uncertain else [])

    async def _assign(self, workers: list[DropletRecord]) -> list[DropletRecord]:
        """Confirm membership; returns the unconfirmed workers (removed inside scope.py)."""
        waiting = [w for w in workers if w.droplet_id and w.status == "requested"]
        if not waiting:
            return []
        records = {d.droplet_id: d for d in self.record.droplets if d.droplet_id}
        _, failed = await scope.assign_and_verify_project(
            self.cloud,
            self.scope,
            [w.droplet_id for w in waiting],
            records,
            audit_path(self.root),
            retry=scope.Retry(sleep=self.clock.sleep, save=self.save),
        )
        self.save()
        for worker in waiting:
            if worker.droplet_id in failed:
                self.alert("high", "project_assign_failed", worker.name)
            if worker.status == "destroy_refused":
                self.alert("high", "destroy_refused", worker.name)
        return [w for w in waiting if w.droplet_id in failed]

    async def _bring_up(self, worker: DropletRecord) -> None:
        """Ready, push and arm one confirmed worker; any error fails only this worker."""
        try:
            await self._start(worker)
        except Exception as error:
            if worker.armed_at is None:  # an armed worker is never start_failed (m10)
                worker.error_detail = scope.error_detail(self.cloud, [worker.droplet_id], error)
                await self._fail(worker, "start_failed", type(error).__name__)

    async def _start(self, worker: DropletRecord) -> None:
        resumed = worker.status == "starting"
        worker.status = "booting"
        self.save()
        droplet = await self._active(worker)
        limit = moment(worker.requested_at).timestamp() + READY_LIMIT_SECONDS - self.clock.now()
        if droplet is None or not await self.remote.wait_ready(droplet, max(limit, 1.0)):
            await self._fail(worker, "boot_failed")
            return
        worker.status, worker.ssh_ready_at = "starting", self.now()
        self.save()
        if not await self._push(worker, droplet, resumed):
            await self._fail(worker, "start_failed")
            return
        worker.status, worker.armed_at = "armed", self.now()
        if worker.replaced_from is not None:  # its round starts now: record the extra delay
            self.record.replacements[cell_key(worker.network, worker.region)][
                "extra_delay_seconds"
            ] = round(max(0.0, self.clock.now() - self.scheduled), 3)
        self.save()

    async def _listing(self) -> list[Droplet]:
        """One list by hour tag per poll interval, shared by every waiting worker."""
        async with self.list_lock:
            if self.clock.now() - self.listed_at >= READY_POLL_SECONDS:
                self.listed = await self.cloud.list_tag(self.tag)
                self.listed_at = self.clock.now()
            return self.listed

    async def _active(self, worker: DropletRecord) -> Droplet | None:
        deadline = moment(worker.requested_at).timestamp() + READY_LIMIT_SECONDS
        while True:
            found = next((d for d in await self._listing() if d.id == worker.droplet_id), None)
            if found is not None and found.status == "active" and found.public_ipv4:
                worker.active_at = worker.active_at or self.now()
                self.hosts[found.id] = found.public_ipv4
                atomic_json(hosts_path(self.root, self.record.hour), self.hosts)
                return found
            if self.clock.now() + READY_POLL_SECONDS > deadline:
                return None
            await self.clock.sleep(READY_POLL_SECONDS)

    async def _push(self, worker: DropletRecord, droplet: Droplet, resumed: bool) -> bool:
        """Push and start, retried once. False only when the round provably never started; a
        started or unreadable worker is armed (`start_unconfirmed`), never start_failed (m9)."""
        manifest = manifest_path(self.root, self.spec, worker.network)
        command = start_command(manifest.parent.name, worker.region, worker.hour)
        payload = self._bundle(worker)
        if resumed and await self._started(droplet):
            return True
        for _ in range(2):
            try:
                await self.remote.push_start(droplet, payload, command)
                return True
            except Exception:
                started = await self._started(droplet)
                if started is not False:
                    worker.error_class = None if started else "start_unconfirmed"
                    return True
        return False

    async def _started(self, droplet: Droplet) -> bool | None:
        """Whether the round unit was ever started; None when the worker cannot be read."""
        try:
            info = await self.remote.inspect(droplet)
        except Exception:
            return None
        unit = (info.get("service") or {}).get("ActiveState", "inactive")
        return "assignment" in info or unit != "inactive"  # the fleet marker, or any unit (m9)

    def _bundle(self, worker: DropletRecord) -> bytes:
        """One tar: the network's manifest, run config, assignment, wheel and credentials."""
        manifest = manifest_path(self.root, self.spec, worker.network)
        run = f"run/{manifest.parent.name}"
        assignment = {
            "campaign_id": self.spec.campaign_id,
            "network": worker.network,
            "region": worker.region,
            "round": worker.hour,
            "name": worker.name,
            "scheduled_at": self.record.scheduled_at,
        }
        files = [
            (f"{run}/campaign.json", manifest.read_bytes()),
            (f"{run}/provider.toml", (manifest.parent / "provider.toml").read_bytes()),
            (f"{run}/assignment.json", json.dumps(assignment).encode()),
            (f"wheel/{self.inputs.wheel.name}", self.inputs.wheel.read_bytes()),
            (
                "secrets/credentials.toml",
                (self.inputs.credentials_dir / f"{worker.network}.toml").read_bytes(),
            ),
        ]
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for name, data in files:
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = len(data), 0o600, int(self.clock.now())
                archive.addfile(info, io.BytesIO(data))
        return buffer.getvalue()

    async def _fail(self, worker: DropletRecord, status: str, cause: str | None = None) -> None:
        """Remove a failed worker through the guard, then apply the replacement rule. Our own
        new worker whose membership was never confirmed takes the own-unassigned path: the
        plain guard needs project membership and would refuse it, leaving it alive."""
        worker.status, worker.error_class = status, cause or status
        self.alert("high", status, worker.name)
        if worker.assigned_at is None and worker.droplet_id in self.cloud.created_ids:
            await self._unassigned(worker)
        else:
            await self.retire(worker)
        self.save()
        await self._replace(worker)

    async def _unassigned(self, worker: DropletRecord) -> None:
        records = {d.droplet_id: d for d in self.record.droplets if d.droplet_id}
        await scope.remove_unassigned(
            self.cloud,
            self.scope,
            records,
            audit_path(self.root),
            {worker.droplet_id: worker.error_detail},
            save=self.save,
        )
        if worker.status == "destroy_refused":
            self.alert("high", "destroy_refused", worker.name)
        elif worker.status == "destroyed":
            self.add_hours(worker)

    async def _replace(self, worker: DropletRecord) -> None:
        """Replace a failed worker in the same hour: at most `REPLACEMENTS_PER_CELL` times per
        cell, only before `replace_until`. Past either bound the cell is lost
        (`failed_ours`, from the newest record) with the high alert `cell_failed`."""
        tries = sum(d.name == worker.name for d in self.record.droplets)
        if tries > REPLACEMENTS_PER_CELL or self.clock.now() >= self.replace_until:
            self.alert("high", "cell_failed", worker.name)
            return
        cell = self.record.replacements.setdefault(
            cell_key(worker.network, worker.region),
            {"replaced_from": [], "extra_delay_seconds": None},
        )
        cell["replaced_from"].append(
            {
                "droplet_id": worker.droplet_id,
                "status": worker.status,
                "error_class": worker.error_class,
            }
        )
        fresh = self._worker(worker.network, worker.region, worker.overlap, worker.concurrency)
        fresh.replaced_from = next(i for i, d in enumerate(self.record.droplets) if d is worker)
        self.record.droplets.append(fresh)
        self.save()
        await self._region([fresh])
