"""The always-on collector: harvest every live hour, finalize, reconcile orphans, write status.

Every 30 s it harvests every hour in `running` or `harvesting` (hours overlap freely), writes the
traffic report of each newly finished hour, reconciles orphans every 5 min through
`scope.guarded_delete` only, raises the section 9 and 11 alerts and writes `status.json`.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path

from ..onboarding.files import atomic_json, fingerprint
from .contracts import (
    CampaignSpec,
    Cloud,
    Droplet,
    DropletRecord,
    Harvester,
    HourRecord,
    Remote,
    audit_path,
    campaign_tag,
    runtime_dir,
    status_path,
)
from .reports import latest_summary, write_traffic_report
from .scope import Ledger, OwnershipError, guarded_delete, role_checks, scope_for
from .tick import (
    FINAL,
    LAUNCHABLE,
    Clock,
    alert,
    due_at,
    load_hours,
    load_spec,
    read_alerts,
    save_hour,
    utc_now,
)

MANAGER_ROOT = Path("/opt/proxy-index-campaign")
HARVESTED = ("running", "harvesting")
ROUND_LIVE = ("armed", "running")
LONG_ROUND = ((3, "info"), (12, "high"))  # hours, level (D4: never stopped automatically)
DISK_FLOOR_PCT, RATELIMIT_FLOOR, CREATE_FAIL_SHARE = 20.0, 500, 0.10
RemoteFactory = Callable[[int], Remote]


def live(record: DropletRecord) -> bool:
    """The droplet may still exist: created and not yet destroyed."""
    return (record.droplet_id is not None and record.destroyed_at is None
            and record.status not in ("destroyed", "create_failed"))


def ntp_synced() -> bool | None:
    try:
        reply = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                               capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return {"yes": True, "no": False}.get(reply.stdout.strip())


@dataclass
class Pacing:
    interval: float = 30.0
    reconcile_every: float = 300.0
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    clock_synced: Callable[[], bool | None] = field(default_factory=lambda: ntp_synced)


@dataclass
class Collector:
    root: Path
    harvester: Harvester
    cloud: Cloud | None
    remote_factory: RemoteFactory | None
    clock: Clock
    pacing: Pacing
    reconciled_at: datetime | None = None

    async def pass_once(self) -> dict:
        spec = load_spec(self.root)
        hours = load_hours(self.root, spec)
        for slot, record in hours.items():
            if record.state in HARVESTED:
                hours[slot] = await self._harvest(spec, record)
        now = self.clock()
        if not spec.synthetic and self.cloud is not None and (
                self.reconciled_at is None
                or (now - self.reconciled_at).total_seconds() >= self.pacing.reconcile_every):
            await self.reconcile(spec, hours)
            self.reconciled_at = now
        for record in hours.values():
            self._finalize(record)
        self._alerts(spec, hours, now)
        status = self._status(spec, hours, now)
        atomic_json(status_path(self.root), status)
        return status

    async def _harvest(self, spec: CampaignSpec, record: HourRecord) -> HourRecord:
        try:
            record = await self.harvester(spec, record, self.root)
        except Exception as error:  # one hour's failure never stops the others
            alert(self.root, self.clock(), "harvest_error", "info",
                  key=f"h{record.hour:02d}-{type(error).__name__}", hour=record.hour,
                  error=type(error).__name__)
            return record
        save_hour(self.root, record)
        return record

    def _finalize(self, record: HourRecord) -> None:
        if record.state not in FINAL or record.report_path:
            return
        record.finished_at = record.finished_at or self.clock().isoformat()
        save_hour(self.root, record)
        path = write_traffic_report(self.root, record.hour)
        record.report_path = path.relative_to(self.root).as_posix()
        save_hour(self.root, record)

    async def reconcile(self, spec: CampaignSpec, hours: dict[int, HourRecord]) -> None:
        """Section 9: adopt unrecorded owned droplets, retire `destroyed` ones that remain."""
        scope = scope_for(spec)
        known = {d.droplet_id: (record, d) for record in hours.values() for d in record.droplets
                 if d.droplet_id}
        found = await self.cloud.list_tag(campaign_tag(spec.campaign_id))
        members = await self.cloud.project_droplet_ids(spec.project_id)
        for droplet in found:
            checks = role_checks(droplet, scope, "worker")
            hour = int(droplet.name.split("-")[2][1:]) if checks["name"] else None
            if hour in hours and hours[hour].state in LAUNCHABLE:
                continue  # the tick owns a launching hour; its creates may not be recorded yet
            if hour not in hours or not all(checks.values()) or droplet.id not in members:
                alert(self.root, self.clock(), "foreign_tagged", key=droplet.id,
                      droplet_id=droplet.id, name=droplet.name)
            elif droplet.id not in known:
                await self._adopt(scope, hours[hour], droplet, hours)
            elif known[droplet.id][1].status == "destroyed":
                await self._retire(scope, *known[droplet.id], hours)

    async def _adopt(self, scope, record: HourRecord, droplet: Droplet, hours) -> None:
        _, _, _, region, network = droplet.name.split("-", 4)
        try:
            inspected = await self.remote_factory(record.hour).inspect(droplet)
        except Exception:
            inspected = {"error": "inspect failed"}
        if "error" in inspected:
            alert(self.root, self.clock(), "orphan_unreachable", "info", key=droplet.id,
                  droplet_id=droplet.id, name=droplet.name)
            return
        drop = DropletRecord(network, region, record.hour, droplet.name, droplet.id,
                             size=droplet.size, status="running",
                             requested_at=droplet.created_at.isoformat())
        record.droplets.append(drop)
        alert(self.root, self.clock(), "orphan_adopted", "info", key=droplet.id,
              droplet_id=droplet.id, name=droplet.name, active="assignment" in inspected)
        if "assignment" in inspected:  # an active-run marker: harvest it normally
            if record.state in FINAL:
                record.state, record.report_path = "harvesting", None
            save_hour(self.root, record)
            return
        drop.error_class = "orphan_not_started"
        save_hour(self.root, record)
        await self._retire(scope, record, drop, hours)

    async def _retire(self, scope, record: HourRecord, drop: DropletRecord, hours) -> None:
        ledger = Ledger({"droplet": [d.droplet_id for r in hours.values() for d in r.droplets
                                     if d.droplet_id]},
                        {d.droplet_id: d for r in hours.values() for d in r.droplets
                         if d.droplet_id})
        try:
            await guarded_delete(self.cloud, "droplet", drop.droplet_id, scope, ledger,
                                 audit_path(self.root))
            drop.status, drop.destroyed_at = "destroyed", self.clock().isoformat()
        except OwnershipError:
            drop.status = "destroy_refused"
            alert(self.root, self.clock(), "destroy_refused", key=drop.droplet_id,
                  droplet_id=drop.droplet_id, name=drop.name)
        except Exception as error:  # audited by the guard; retried at the next reconcile
            drop.status = "destroy_failed"
            alert(self.root, self.clock(), "destroy_failed", "info",
                  key=f"{drop.droplet_id}-{type(error).__name__}", droplet_id=drop.droplet_id)
        save_hour(self.root, record)

    def _alerts(self, spec: CampaignSpec, hours: dict[int, HourRecord], now: datetime) -> None:
        stamp = now.strftime("%Y%m%dT%H")
        for record in hours.values():
            for item in record.alerts:  # launch and harvest alerts, mirrored once each
                alert(self.root, now, str(item.get("code", "hour_alert")),
                      str(item.get("level", "info")), key=f"h{record.hour:02d}-"
                      f"{fingerprint(item)[:16]}", hour=record.hour)
            self._long_rounds(record, now)
            failed = sum(d.status == "create_failed" for d in record.droplets)
            if record.droplets and failed > CREATE_FAIL_SHARE * len(record.droplets):
                alert(self.root, now, "create_failures", key=f"h{record.hour:02d}",
                      hour=record.hour, failed=failed, total=len(record.droplets))
        remaining = self._ratelimit()
        checks = {"disk_low": self._disk_free_pct() < DISK_FLOOR_PCT,
                  "clock_unsynced": self.pacing.clock_synced() is False,
                  "ratelimit_low": remaining is not None and remaining < RATELIMIT_FLOOR}
        for code in [code for code, raised in checks.items() if raised]:
            alert(self.root, now, code, key=f"{code}-{stamp}")

    def _long_rounds(self, record: HourRecord, now: datetime) -> None:
        for drop in record.droplets:
            started = drop.started_at or record.scheduled_at
            if drop.status not in ROUND_LIVE or drop.finished_at:
                continue
            age = now - datetime.fromisoformat(started)
            for limit, level in LONG_ROUND:
                if age >= timedelta(hours=limit):
                    alert(self.root, now, "long_round", level, key=f"{drop.name}-{limit}h",
                          hour=record.hour, name=drop.name, hours=limit)

    def _disk_free_pct(self) -> float:
        usage = shutil.disk_usage(self.root)
        return round(100 * usage.free / usage.total, 1)

    def _ratelimit(self) -> int | None:
        return None if self.cloud is None else self.cloud.ratelimit_remaining

    def _status(self, spec: CampaignSpec, hours: dict[int, HourRecord], now: datetime) -> dict:
        rows = [{"hour": slot, "state": record.state,
                 "launch_delay_seconds": record.launch_delay_seconds,
                 "cells": dict(Counter(record.cells.values())),
                 "live_workers": sum(map(live, record.droplets))}
                for slot, record in sorted(hours.items())]
        planned = [slot for slot, record in hours.items() if record.state == "planned"]
        workers = sum(row["live_workers"] for row in rows)
        return {
            "campaign_id": spec.campaign_id, "now": now.isoformat(),
            "next_launch_at": due_at(spec, min(planned)).isoformat() if planned else None,
            "hours": rows, "live_workers": workers,
            "alerts_open": sum(item.get("level") == "high" for item in read_alerts(self.root)),
            "traffic_latest": latest_summary(hours), "ratelimit_remaining": self._ratelimit(),
            "disk_free_pct": self._disk_free_pct(), "clock_synced": self.pacing.clock_synced(),
            "finished": workers == 0 and all(r.state in FINAL for r in hours.values()),
        }


def settled(status: dict) -> bool:
    """`--until-done`: every launched hour is final and no worker is left (the dry run)."""
    launched = [row for row in status["hours"] if row["state"] != "planned"]
    return bool(launched) and status["live_workers"] == 0 and all(
        row["state"] in FINAL for row in launched)


async def run_collector(root: Path, *, harvester: Harvester, cloud: Cloud | None,
                        remote_factory: RemoteFactory | None, clock: Clock = utc_now,
                        until_done: bool = False, pacing: Pacing | None = None) -> dict:
    """Loop until every hour is final and no worker is alive (or, `until_done`, `settled`)."""
    pacing = pacing or Pacing()
    collector = Collector(root, harvester, cloud, remote_factory, clock, pacing)
    while True:
        status = await collector.pass_once()
        if status["finished"] or (until_done and settled(status)):
            return status
        await pacing.sleep(pacing.interval)


def services(root: Path, spec: CampaignSpec):
    """The manager's client, SSH remotes, launcher and harvester (CAMP-1c; CAMP-1g layout)."""
    from .do_api import DigitalOcean, file_token
    from .fanout import OpenSSHRemote
    from .harvest import harvest_hour
    from .launch import launch_hour

    secrets = MANAGER_ROOT / ".secrets"
    cloud = DigitalOcean(file_token(secrets / "do-token"))

    def remote_factory(hour: int) -> Remote:
        return OpenSSHRemote(secrets / "worker-key", runtime_dir(root), hour)

    common = {"cloud": cloud, "remote_factory": remote_factory, "clock": utc_now}
    return cloud, remote_factory, partial(launch_hour, **common), partial(harvest_hour, **common)


def _collector_command(args) -> int:
    root = Path(args.root)
    cloud, remote_factory, _, harvester = services(root, load_spec(root))

    async def main() -> dict:
        try:
            return await run_collector(root, harvester=harvester, cloud=cloud,
                                       remote_factory=remote_factory, until_done=args.until_done)
        finally:
            await cloud.close()

    status = asyncio.run(main())
    print(json.dumps({"finished": status["finished"], "live_workers": status["live_workers"]}))
    return 0


def register(subcommands, handlers: dict) -> None:
    collector = subcommands.add_parser("collector", help="Harvest, finalize and reconcile (daemon)")
    collector.add_argument("--root", required=True)
    collector.add_argument("--until-done", action="store_true",
                           help="exit once every launched hour is final (dry run)")
    handlers.update(collector=_collector_command)
