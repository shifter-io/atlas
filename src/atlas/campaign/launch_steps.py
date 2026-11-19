"""The per-worker launch steps of `launch._Launch`: wait for readiness, push and arm one
confirmed worker, and fail, remove or replace it (moved out of `launch.py`, FILE-16)."""

from __future__ import annotations

import io
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path

from ..onboarding.files import atomic_json, digest
from ..onboarding.manifest import moment
from . import scope
from .contracts import (
    CampaignSpec,
    Droplet,
    DropletRecord,
    audit_path,
    campaign_tag,
    runtime_dir,
    tags,
)
from .do_api import CreateError
from .fanout import READY_POLL_SECONDS, RemoteError, boot_reason, start_command
from .sizes import size_unavailable

READY_LIMIT_SECONDS = 180
REPLACEMENTS_PER_CELL = 3


@dataclass(frozen=True)
class WorkerInputs:
    """Manager-side files a worker push reads: the campaign wheel and per-network credentials."""

    wheel: Path
    credentials_dir: Path

    @classmethod
    def for_root(cls, root: Path, wheel_sha256: str) -> WorkerInputs:
        """`<base>/campaigns/<cid>`: the spec's wheel in `<base>/wheels` (or a `<sha12>/`
        folder there, where the server setup puts the current wheel), `<base>/.secrets`."""
        base = root.parent.parent
        found = sorted((base / "wheels").glob("*.whl")) + sorted((base / "wheels").glob("*/*.whl"))
        wheels = [w for w in found if digest(w) == wheel_sha256]
        if not wheels:
            raise ValueError("no manager wheel matches the campaign spec")
        return cls(wheels[0], base / ".secrets" / "credentials")


def cell_key(network: str, region: str) -> str:
    return f"{network}/{region}"


def manifest_path(root: Path, spec: CampaignSpec, network: str) -> Path:
    return root / spec.manifests[network]


def hosts_path(root: Path, hour: int) -> Path:
    return runtime_dir(root) / f"hosts-{hour:02d}.json"


class WorkerSteps:
    """`_Launch`'s steps for one worker; they use its record, cloud, remote, clock and the
    `HourWork` helpers, and `_region` and `_worker` for a replacement."""

    def _size(self, region: str) -> str:
        """The size a new create in the region uses: the furthest of its ordered sizes given to a
        worker of this hour (each earlier one was refused as not available), else the first."""
        sizes = self.spec.sizes(region)
        given = [d.size for d in self.record.droplets if d.region == region and d.size in sizes]
        return sizes[max(map(sizes.index, given), default=0)]

    async def _create_call(self, workers: list[DropletRecord]) -> list[Droplet]:
        """Create the workers with the region's size; on DigitalOcean's 'size not available',
        the rest with each next size of the region's list (LAUNCHER-SIZE-FALLBACK-1). A size
        used other than the first raises one high alert naming the region and that size."""
        region, created, pending = workers[0].region, [], [w.name for w in workers]
        sizes = self.spec.sizes(region)
        for size in sizes[sizes.index(self._size(region)) :]:
            for worker in (w for w in workers if w.name in pending):
                worker.size = size  # the record holds the size of its create
            try:
                created += await self._create_size(pending, region, size)
            except CreateError as error:
                created += error.created
                if error.uncertain:
                    return created + await self._adopted(error.pending)
                if size_unavailable(error):
                    pending = error.pending
                    continue
                return created
            if size != sizes[0]:
                refused = list(sizes[: sizes.index(size)])
                self.alert("high", "size_fallback", f"{region}/{size}", refused=refused)
            break
        return created

    async def _create_size(self, names: list[str], region: str, size: str) -> list[Droplet]:
        worker = tags(self.spec.environment)["worker"]  # CAMP-STAGING-1: its environment's
        return await self.cloud.create(
            names,
            region=region,
            size=size,
            image=self.spec.image_by_region[region],
            tags=[worker, campaign_tag(self.spec.campaign_id), self.tag],
            ssh_keys=[self.spec.ssh_key_id],
            monitoring=False,
            with_droplet_agent=False,
            ipv6=False,
        )

    async def _bring_up(self, worker: DropletRecord) -> None:
        """Ready, push and arm one confirmed worker; any error fails only this worker."""
        try:
            await self._start(worker)
        except Exception as error:  # boundary: an error fails only this worker
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
            worker.error_detail = {"reason": boot_reason(self.remote, droplet)}  # #1126
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
            except (RemoteError, OSError):  # ssh or its process
                started = await self._started(droplet)
                if started is not False:
                    worker.error_class = None if started else "start_unconfirmed"
                    return True
        return False

    async def _started(self, droplet: Droplet) -> bool | None:
        """Whether the round unit was ever started; None when the worker cannot be read."""
        try:
            info = await self.remote.inspect(droplet)
        except (RemoteError, OSError, ValueError):  # ssh, its process, or a reply not JSON
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
