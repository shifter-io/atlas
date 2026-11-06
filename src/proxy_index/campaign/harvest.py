"""Harvest one hour: one idempotent pass over its live workers.

Each live worker is inspected; a finished round is pulled, verified with `rounds.verify_round`,
its journal saved and its metrics recorded, and only then is the worker removed through
`scope.guarded_delete`. Data comes first: a worker whose data cannot be collected is kept and
alerted, never removed. Rounds are never stopped here (D4), only alerted when long.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from ..onboarding.files import atomic_json, lock, read_json
from ..onboarding.manifest import moment
from ..onboarding.rounds import RoundDataError, round_directory, verify_round
from ..onboarding.scheduler import Clock
from . import sampler
from .contracts import (
    WORKER_ROOT,
    CampaignSpec,
    Droplet,
    DropletRecord,
    HourRecord,
    hour_path,
    logs_dir,
    runtime_dir,
)
from .do_api import DigitalOcean
from .launch import (
    SYNTHETIC_MINUTES,
    HourWork,
    RemoteFactory,
    cell_key,
    manifest_path,
    read_hosts,
)

PULL_RETRY_SECONDS = 300
KEEP_ALERT_SECONDS = 30 * 60
LONG_ROUND = ((3 * 3600, "info", "round_long_3h"), (12 * 3600, "high", "round_long_12h"))
ACTIVE = ("armed", "running", "finished", "collect_failed", "collected", "destroy_failed")
FINAL = frozenset({"destroyed", "create_failed", "destroy_refused"})
STOPPED_OK = (0, 130)  # finished, or stopped by the operator
ROUND_DONE = ("completed", "stopped", "failed")


def harvest_state_path(root: Path, hour: int) -> Path:
    """Retry clocks per droplet (unreachable since, last pull); operational, never downloaded."""
    return runtime_dir(root) / f"harvest-{hour:02d}.json"


def round_ended(info: dict) -> tuple[bool, int | None]:
    """(ended, exit code) from `fleet.REMOTE_INSPECT`; a vanished unit mid-round is exit 1."""
    service = info.get("service") or {}
    active, sub = service.get("ActiveState"), service.get("SubState")
    if active in ("activating", "deactivating", "reloading") or (
        active == "active" and sub != "exited"
    ):
        return False, None
    code = int(service.get("ExecMainStatus") or 1)
    if (info.get("state") or {}).get("status") not in ROUND_DONE:
        code = code or 1
    return True, code


async def harvest_hour(
    spec: CampaignSpec,
    record: HourRecord,
    root: Path,
    *,
    cloud: DigitalOcean | None,
    remote_factory: RemoteFactory | None,
    clock: Clock | None = None,
) -> HourRecord:
    """One pass over the live workers of a launched hour; safe to repeat every 30 s."""
    with lock(hour_path(root, record.hour).parent / ".lock"):
        clients = _Clients(cloud, remote_factory, clock or Clock())
        await _Harvest(spec, record, root, clients).run()
    return record


@dataclass(frozen=True)
class _Clients:
    """The cloud, the per-hour SSH remote factory and the clock of one harvest pass."""

    cloud: DigitalOcean | None
    remote_factory: RemoteFactory | None
    clock: Clock


class _Harvest(HourWork):
    def __init__(self, spec: CampaignSpec, record: HourRecord, root: Path, clients: _Clients):
        super().__init__(spec, record, root, clients.cloud, clients.clock)
        self.remote = None if spec.synthetic else clients.remote_factory(record.hour)
        self.hosts = read_hosts(root, record.hour)
        path = harvest_state_path(root, record.hour)
        self.memo = read_json(path) if path.exists() else {}

    async def run(self) -> None:
        if self.record.state not in ("running", "harvesting"):
            return
        live = [d for d in self.record.droplets if d.status in ACTIVE]
        await asyncio.gather(*(self._worker(d) for d in live))
        statuses = {d.status for d in self.record.droplets}
        if statuses <= FINAL:
            self.record.state = "failed" if "destroy_refused" in statuses else "done"
            self.record.finished_at = self.record.finished_at or self.now()
        elif statuses & {"finished", "collect_failed", "collected", "destroy_failed"}:
            self.record.state = "harvesting"
        self.save()
        atomic_json(harvest_state_path(self.root, self.record.hour), self.memo)

    async def _worker(self, worker: DropletRecord) -> None:
        """One worker's step; an unexpected error is alerted and stays with this worker."""
        try:
            if self.spec.synthetic:
                self._synthetic(worker)
                return
            droplet = Droplet(
                worker.droplet_id,
                worker.name,
                worker.region,
                worker.size,
                "active",
                moment(worker.requested_at or self.record.scheduled_at),
                (),
                public_ipv4=self.hosts.get(str(worker.droplet_id)),
            )
            if worker.status in ("armed", "running") and not await self._watch(worker, droplet):
                return
            if worker.status in ("finished", "collect_failed") and not await self._collect(
                worker, droplet
            ):
                return
            await self.retire(worker)
        except Exception as error:
            worker.error_class = worker.error_class or type(error).__name__
            self.alert("high", "harvest_error", worker.name)
        finally:
            self.save()

    def _synthetic(self, worker: DropletRecord) -> None:
        if self.clock.now() < moment(self.record.scheduled_at).timestamp() + (
            SYNTHETIC_MINUTES * 60
        ):
            return
        now = self.now()
        worker.started_at = worker.started_at or self.record.scheduled_at
        worker.finished_at = worker.collected_at = worker.destroyed_at = now
        worker.status, worker.exit_code = "destroyed", 0
        self.record.cells[cell_key(worker.network, worker.region)] = "complete"
        self.add_hours(worker)

    async def _watch(self, worker: DropletRecord, droplet: Droplet) -> bool:
        """Inspect a running worker; True once its round unit has ended."""
        memo = self.memo.setdefault(str(worker.droplet_id), {})
        try:
            info = await self.remote.inspect(droplet)
        except Exception:
            since = memo.setdefault("unreachable_since", self.clock.now())
            if self.clock.now() - since >= KEEP_ALERT_SECONDS:
                self.alert("high", "worker_unreachable", worker.name)
            return False
        memo.pop("unreachable_since", None)
        self._progress(worker, info.get("state") or {})
        ended, code = round_ended(info)
        if ended:
            worker.status, worker.finished_at, worker.exit_code = "finished", self.now(), code
        return ended

    def _progress(self, worker: DropletRecord, state: dict) -> None:
        started = next(
            (j["started_at"] for j in state.get("jobs", []) if j.get("started_at")), None
        )
        if started and not worker.started_at:
            worker.status, worker.started_at = "running", started
            worker.start_lateness_seconds = round(
                moment(started).timestamp() - moment(self.record.scheduled_at).timestamp(), 3
            )
        if worker.started_at:
            age = self.clock.now() - moment(worker.started_at).timestamp()
            for limit, level, kind in LONG_ROUND:
                if age >= limit:
                    self.alert(level, kind, worker.name)

    async def _collect(self, worker: DropletRecord, droplet: Droplet) -> bool:
        """Pull, verify, journal and record; a failure keeps the worker and re-pulls later."""
        memo, now = self.memo.setdefault(str(worker.droplet_id), {}), self.clock.now()
        if worker.status == "collect_failed" and now - memo.get("last_pull", 0) < (
            PULL_RETRY_SECONDS
        ):  # the memo may predate a restart (m7)
            return False
        memo["last_pull"] = now
        manifest = manifest_path(self.root, self.spec, worker.network)
        local = round_directory(manifest, worker.region, worker.hour)
        remote = (
            f"{WORKER_ROOT}/run/{manifest.parent.name}/regions/{worker.region}"
            f"/rounds/{worker.hour:02d}"
        )
        try:
            await self.remote.pull(droplet, remote, local)
            result = self._verify(manifest, worker)
        except Exception:
            worker.status, worker.error_class = "collect_failed", "collect_failed"
            if now - memo.setdefault("collect_failed_since", now) >= KEEP_ALERT_SECONDS:
                self.alert("high", "collect_failed", worker.name)
            return False
        try:
            await self.remote.journal(
                droplet,
                logs_dir(self.root, worker.hour) / f"{worker.network}-{worker.region}.journal.txt",
            )
        except Exception:
            self.alert("info", "journal_failed", worker.name)
        self._account(worker, result, local)
        worker.status, worker.collected_at = "collected", self.now()
        return True

    def _verify(self, manifest: Path, worker: DropletRecord) -> dict | None:
        """The round's verification; data defects of a finished round raise (re-pull)."""
        try:
            result = verify_round(manifest, worker.region, worker.hour)
        except (OSError, ValueError, KeyError):
            if worker.exit_code in STOPPED_OK:
                raise
            return None  # a failed unit may leave no checkpoint; its data is partial anyway
        explained = {
            f"{c['country']}: {status}"
            for c in result["countries"]
            for status in (c["status"], "not started")
        }
        if worker.exit_code in STOPPED_OK and set(result["issues"]) - explained:
            raise RoundDataError("round data does not verify")
        return result

    def _account(self, worker: DropletRecord, result: dict | None, local: Path) -> None:
        """Record counters, CPU samples, the cell and the network's traffic."""
        countries = result["countries"] if result else []
        worker.requests = sum(c["requests"] for c in countries)
        worker.successes = sum(c["successes"] for c in countries)
        worker.errors = sum(c["errors"] for c in countries)
        worker.bytes_up = sum(c["bytes_up"] for c in countries)
        worker.bytes_down = sum(c["bytes_down"] for c in countries)
        cpu = sampler.summarize(
            local / "host-samples.jsonl",
            since=moment(worker.started_at).timestamp() if worker.started_at else None,
        )
        worker.cpu_p50_pct, worker.cpu_p95_pct = cpu["cpu_p50_pct"], cpu["cpu_p95_pct"]
        worker.cpu_max_pct, worker.cpu_peak_pct = cpu["cpu_max_pct"], cpu["cpu_peak_pct"]
        worker.cpu_core_max_pct, worker.steal_p95_pct = (
            cpu["cpu_core_max_pct"],
            cpu["steal_p95_pct"],
        )
        worker.load1_p95, worker.mem_used_max_pct = cpu["load1_p95"], cpu["mem_used_max_pct"]
        worker.rss_peak_mb = cpu["rss_peak_mb"]
        ok = worker.exit_code in STOPPED_OK
        worker.error_class = None if ok else "round_failed"
        self.record.cells[cell_key(worker.network, worker.region)] = (
            "complete"
            if ok and worker.exit_code == 0 and result and result["complete"]
            else "partial"
            if ok
            else "failed_ours"
        )
        traffic = self.record.traffic[worker.network]
        for key, value in (
            ("requests", worker.requests),
            ("successes", worker.successes),
            ("errors", worker.errors),
            ("bytes_up", worker.bytes_up),
            ("bytes_down", worker.bytes_down),
            ("countries_done", sum(c["status"] == "completed" for c in countries)),
        ):
            traffic[key] += value
