"""Launch one hour: create, confirm project membership, wait for readiness, push and arm.

Names are persisted before the create and ids right after it. Only workers whose membership in
project Proxy Index is confirmed are ever pushed (fail-closed). A boot, SSH, start or assignment
failure gets one replacement before HH:00 + `replace_until_minutes`. Every removal goes through
`scope.guarded_delete`; a resumed launch adopts its own names before it creates anything.
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
from ..onboarding.manifest import moment, timestamp
from ..onboarding.scheduler import Clock
from . import scope
from .contracts import (
    WORKER_TAG,
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
)
from .do_api import CreateError, DigitalOcean
from .fanout import start_command

READY_LIMIT_SECONDS = 180
READY_POLL_SECONDS = 2.0
CREATE_RETRY_SECONDS = 15
SYNTHETIC_MINUTES = 5
LIVE = frozenset({"armed", "running"})
UP = ("requested", "assigned", "booting", "starting")
RemoteFactory = Callable[[int], Remote]
TRAFFIC_KEYS = ("bytes_up", "bytes_down", "requests", "successes", "errors", "countries_done",
                "countries_total", "worker_hours")


@dataclass(frozen=True)
class WorkerInputs:
    """Manager-side files a worker push reads: the campaign wheel and per-network credentials."""

    wheel: Path
    credentials_dir: Path

    @classmethod
    def for_root(cls, root: Path, wheel_sha256: str) -> WorkerInputs:
        """The manager layout `<base>/campaigns/<cid>`: the wheel in `<base>/wheels` whose
        SHA-256 the spec binds, and `<base>/.secrets/credentials/<network>.toml`."""
        base = root.parent.parent
        wheels = [w for w in sorted((base / "wheels").glob("*.whl"))
                  if digest(w) == wheel_sha256]
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


def latest(record: HourRecord) -> dict[str, DropletRecord]:
    """The newest worker record per cell (a replacement supersedes the worker it replaces)."""
    return {cell_key(d.network, d.region): d for d in record.droplets}


class HourWork:
    """One pass over one hour's record: saving, alerts and guarded removal."""

    def __init__(self, spec: CampaignSpec, record: HourRecord, root: Path,
                 cloud: DigitalOcean | None, clock: Clock):
        self.spec, self.record, self.root, self.cloud, self.clock = spec, record, root, cloud, clock

    def now(self) -> str:
        return timestamp(self.clock.now())

    def save(self) -> None:
        atomic_json(hour_path(self.root, self.record.hour), self.record.to_dict())

    def alert(self, level: str, kind: str, name: str | None = None) -> None:
        """One alert per kind and droplet name; alerts hold names, never addresses."""
        if not any(a["kind"] == kind and a.get("name") == name for a in self.record.alerts):
            self.record.alerts.append({"time": self.now(), "level": level, "kind": kind,
                                       "name": name})

    async def retire(self, worker: DropletRecord) -> None:
        """Remove one worker through the guard; a refusal is final, an error retried later."""
        workers = [d for d in self.record.droplets if d.droplet_id]
        ledger = scope.Ledger({"droplet": [d.droplet_id for d in workers]},
                              {d.droplet_id: d for d in workers})
        try:
            await scope.guarded_delete(self.cloud, "droplet", str(worker.droplet_id),
                                       scope.scope_for(self.spec), ledger, audit_path(self.root))
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
            max(0.0, self.clock.now() - start) / 3600, 4)


async def launch_hour(spec: CampaignSpec, record: HourRecord, root: Path, *,
                      cloud: DigitalOcean | None, remote_factory: RemoteFactory | None,
                      clock: Clock | None = None) -> HourRecord:
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
        self.replace_until = self.scheduled + spec.replace_until_minutes * 60

    async def run(self) -> None:
        record = self.record
        if record.state not in ("planned", "launching"):
            return
        record.state, record.fired_at = "launching", record.fired_at or self.now()
        if not record.droplets:
            self._plan_workers()
        self.save()
        if self.spec.synthetic:
            self._synthetic()
        else:
            await self._launch()
        record.cells.update({key: "running" if d.status in LIVE else "failed_ours"
                             for key, d in latest(record).items()})
        record.state, record.launched_at = "running", self.now()
        self.save()

    def _plan_workers(self) -> None:
        spec, record = self.spec, self.record
        earlier = [load_hour(self.root, h) for h in range(record.hour)]
        live = [d for r in earlier if r for d in r.droplets if d.status in LIVE]
        for network in spec.networks:
            countries = read_json(manifest_path(self.root, spec, network))["regions"]
            record.traffic[network] = dict.fromkeys(TRAFFIC_KEYS, 0)
            record.traffic[network]["countries_total"] = sum(
                len(countries[r]) for r in spec.regions)
            for region in spec.regions:
                record.droplets.append(self._worker(network, region, sum(
                    d.network == network and d.region == region for d in live)))
                record.cells[cell_key(network, region)] = "pending"

    def _worker(self, network: str, region: str, overlap: int) -> DropletRecord:
        return DropletRecord(
            network, region, self.record.hour,
            droplet_name(self.spec.campaign_id, self.record.hour, region, network),
            size=self.spec.size_by_region[region], image_id=self.spec.image_by_region[region],
            overlap=overlap, concurrency=self.spec.concurrency)

    def _synthetic(self) -> None:
        now = self.now()
        for index, worker in enumerate(self.record.droplets):
            if worker.status in UP:
                worker.droplet_id = worker.droplet_id or f"synthetic-{index}"
                worker.requested_at = worker.requested_at or now
                worker.status, worker.armed_at = "armed", now

    async def _launch(self) -> None:
        self.inputs = WorkerInputs.for_root(self.root, self.spec.wheel_sha256)
        if self.clock.now() < self.scheduled - self.spec.create_lead_seconds:
            await self.clock.sleep(self.scheduled - self.spec.create_lead_seconds
                                   - self.clock.now())
        await self._adopt()
        await asyncio.gather(*(self._region([d for d in self.record.droplets
                                             if d.region == region and d.status in UP], True)
                               for region in self.spec.regions))

    async def _adopt(self) -> None:
        """A resumed launch: our own recorded names under the hour tag, before any create."""
        known = {d.droplet_id for d in self.record.droplets if d.droplet_id}
        waiting = [d for d in self.record.droplets if d.status in UP and (
            d.droplet_id is None or d.droplet_id not in self.cloud.created_ids)]
        if not waiting:
            return
        found = await self.cloud.adopt_uncertain([d.name for d in waiting], self.tag)
        self._take([d for d in waiting if d.droplet_id is None],
                   [f for f in found if f.id not in known])
        self.save()

    def _take(self, workers: list[DropletRecord], droplets: list[Droplet]) -> None:
        by_name = {}
        for droplet in droplets:
            by_name.setdefault(droplet.name, droplet)
        for worker in workers:
            if worker.droplet_id is None and worker.name in by_name:
                worker.droplet_id = by_name.pop(worker.name).id

    async def _region(self, workers: list[DropletRecord], replace: bool) -> None:
        """One region's workers. An unexpected error stays inside this region, no task outlives
        it, and every worker leaves it armed or in a final status (B1)."""
        cause = None
        try:
            await self._create(workers)
            await self._assign(workers, replace)
        except Exception as error:
            cause = type(error).__name__
        await asyncio.gather(*(self._bring_up(w, replace) for w in workers
                               if w.status in ("assigned", "booting", "starting")))
        left = [w for w in workers if w.status in UP]
        for worker in left:
            self.alert("high", "launch_error", worker.name)
            if worker.droplet_id is None:
                worker.status, worker.error_class = "create_failed", cause
        await asyncio.gather(*(self._fail(w, "start_failed", replace, cause)
                               for w in left if w.droplet_id))
        self.save()

    async def _create(self, workers: list[DropletRecord]) -> None:
        pending = [w for w in workers if w.droplet_id is None and w.status == "requested"]
        for attempt in range(2):
            if not pending:
                return
            if attempt:
                await self.clock.sleep(CREATE_RETRY_SECONDS)
            for worker in pending:
                worker.attempts, worker.requested_at = worker.attempts + 1, timestamp(
                    self.clock.now())
            self.save()
            self._take(pending, await self._create_call(pending))
            self.save()
            pending = [w for w in pending if w.droplet_id is None]
        for worker in pending:
            worker.status, worker.error_class = "create_failed", "create_failed"
            self.alert("high", "create_failed", worker.name)

    async def _create_call(self, workers: list[DropletRecord]) -> list[Droplet]:
        names, region = [w.name for w in workers], workers[0].region
        tags = [WORKER_TAG, campaign_tag(self.spec.campaign_id), self.tag]
        try:
            return await self.cloud.create(
                names, region=region, size=self.spec.size_by_region[region],
                image=self.spec.image_by_region[region], tags=tags,
                ssh_keys=[self.spec.ssh_key_id], monitoring=False, with_droplet_agent=False,
                ipv6=False)
        except CreateError as error:
            if not error.uncertain:
                return error.created
            known = {d.droplet_id for d in self.record.droplets if d.droplet_id}
            found = await self.cloud.adopt_uncertain(error.pending, self.tag)
            return error.created + [d for d in found if d.id not in known]

    async def _assign(self, workers: list[DropletRecord], replace: bool) -> None:
        waiting = [w for w in workers if w.droplet_id and w.status == "requested"]
        if not waiting:
            return
        records = {d.droplet_id: d for d in self.record.droplets if d.droplet_id}
        _, failed = await scope.assign_and_verify_project(
            self.cloud, self.scope, [w.droplet_id for w in waiting], records,
            audit_path(self.root), retry=scope.Retry(sleep=self.clock.sleep))
        self.save()
        for worker in waiting:
            if worker.droplet_id in failed:
                self.alert("high", "project_assign_failed", worker.name)
                await self._replace(worker, replace)

    async def _bring_up(self, worker: DropletRecord, replace: bool) -> None:
        """Ready, push and arm one confirmed worker; any error fails only this worker."""
        try:
            await self._start(worker, replace)
        except Exception as error:
            await self._fail(worker, "start_failed", replace, type(error).__name__)

    async def _start(self, worker: DropletRecord, replace: bool) -> None:
        resumed = worker.status == "starting"
        worker.status = "booting"
        self.save()
        droplet = await self._active(worker)
        limit = moment(worker.requested_at).timestamp() + READY_LIMIT_SECONDS - self.clock.now()
        if droplet is None or not await self.remote.wait_ready(droplet, max(limit, 1.0)):
            await self._fail(worker, "boot_failed", replace)
            return
        worker.status, worker.ssh_ready_at = "starting", self.now()
        self.save()
        if not await self._push(worker, droplet, resumed):
            await self._fail(worker, "start_failed", replace)
            return
        worker.status, worker.armed_at = "armed", self.now()
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
        """Push and start, retried once on the same worker; a resumed push checks first."""
        if resumed:
            try:
                if "assignment" in await self.remote.inspect(droplet):
                    return True
            except Exception:
                pass
        manifest = manifest_path(self.root, self.spec, worker.network)
        command = start_command(manifest.parent.name, worker.region, worker.hour)
        payload = self._bundle(worker)
        for _ in range(2):
            try:
                await self.remote.push_start(droplet, payload, command)
                return True
            except Exception:
                continue
        return False

    def _bundle(self, worker: DropletRecord) -> bytes:
        """One tar: the network's manifest, run config, assignment, wheel and credentials."""
        manifest = manifest_path(self.root, self.spec, worker.network)
        run = f"run/{manifest.parent.name}"
        assignment = {"campaign_id": self.spec.campaign_id, "network": worker.network,
                      "region": worker.region, "round": worker.hour, "name": worker.name,
                      "scheduled_at": self.record.scheduled_at}
        files = [(f"{run}/campaign.json", manifest.read_bytes()),
                 (f"{run}/provider.toml", (manifest.parent / "provider.toml").read_bytes()),
                 (f"{run}/assignment.json", json.dumps(assignment).encode()),
                 (f"wheel/{self.inputs.wheel.name}", self.inputs.wheel.read_bytes()),
                 ("secrets/credentials.toml",
                  (self.inputs.credentials_dir / f"{worker.network}.toml").read_bytes())]
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for name, data in files:
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = len(data), 0o600, int(self.clock.now())
                archive.addfile(info, io.BytesIO(data))
        return buffer.getvalue()

    async def _fail(self, worker: DropletRecord, status: str, replace: bool,
                    cause: str | None = None) -> None:
        """Remove a failed worker through the guard, then apply the replacement rule."""
        worker.status, worker.error_class = status, cause or status
        self.alert("high", status, worker.name)
        await self.retire(worker)
        self.save()
        await self._replace(worker, replace)

    async def _replace(self, worker: DropletRecord, replace: bool) -> None:
        """One replacement per cell, only before HH:00 + replace_until_minutes."""
        tries = sum(d.name == worker.name for d in self.record.droplets)
        if not replace or tries > 1 or self.clock.now() >= self.replace_until:
            return
        fresh = self._worker(worker.network, worker.region, worker.overlap)
        self.record.droplets.append(fresh)
        self.save()
        await self._region([fresh], False)
