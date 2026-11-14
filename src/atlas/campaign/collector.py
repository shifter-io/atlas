"""The always-on collector: every 30 s it harvests every live hour (hours overlap), reports each
newly final hour, reconciles orphans every 5 min (deleting only through `scope.guarded_delete`),
raises the section 9 and 11 alerts and writes `status.json`."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path

from ..onboarding.files import atomic_json, fingerprint
from .collector_reconcile import Reconciler
from .contracts import (
    CampaignSpec,
    Cloud,
    DropletRecord,
    Harvester,
    HourRecord,
    Remote,
    runtime_dir,
    status_path,
)
from .reports import latest_summary, write_traffic_report
from .tick import (
    FINAL,
    AlertLog,
    Clock,
    due_at,
    load_hours,
    load_spec,
    read_alerts,
    save_hour,
    utc_now,
)

# MANAGER-HOST-1: a manager on an existing server names its environment's folder here.
ROOT_ENV = "ATLAS_MANAGER_ROOT"
MANAGER_ROOT = Path(os.environ.get(ROOT_ENV) or "/opt/atlas-campaign")
HARVESTED = ("running", "harvesting")
RECONCILE_HIGH_AFTER = 3  # consecutive failed reconciles before a high alert
DISK_FLOOR_PCT, RATELIMIT_FLOOR, CREATE_FAIL_SHARE = 20.0, 500, 0.10
BOOT_GRACE_SECONDS = 300  # NTP may not have synced yet right after a boot
RemoteFactory = Callable[[int], Remote]


def live(record: DropletRecord) -> bool:
    """The droplet may still exist: created and not yet destroyed."""
    return (
        record.droplet_id is not None
        and record.destroyed_at is None
        and record.status not in ("destroyed", "create_failed")
    )


def ntp_synced() -> bool | None:
    try:
        reply = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return {"yes": True, "no": False}.get(reply.stdout.strip())


def boot_uptime() -> float | None:
    """Seconds since boot from `/proc/uptime`; None when it cannot be read."""
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


@dataclass
class Pacing:
    interval: float = 30.0
    reconcile_every: float = 300.0
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    clock_synced: Callable[[], bool | None] = field(default_factory=lambda: ntp_synced)
    uptime: Callable[[], float | None] = field(default_factory=lambda: boot_uptime)
    disk_usage: Callable[[Path], tuple] = field(default_factory=lambda: shutil.disk_usage)


@dataclass
class Collector(Reconciler):
    root: Path
    harvester: Harvester
    cloud: Cloud | None
    remote_factory: RemoteFactory | None
    clock: Clock
    pacing: Pacing
    reconciled_at: datetime | None = None
    reconcile_failures: int = 0
    failing_since: str = ""
    unreachable: set[str] = field(default_factory=set)  # owned orphans not yet recorded (S5)
    log: AlertLog = field(init=False)

    def __post_init__(self) -> None:
        self.log = AlertLog(self.root)

    async def pass_once(self) -> dict:
        spec = load_spec(self.root)
        hours = load_hours(self.root, spec)
        for slot, record in hours.items():
            if record.state in HARVESTED:
                hours[slot] = await self._harvest(spec, record)
        now = self.clock()
        if (
            not spec.synthetic
            and self.cloud is not None
            and (
                self.reconciled_at is None
                or (now - self.reconciled_at).total_seconds() >= self.pacing.reconcile_every
            )
        ):
            await self._reconcile_safely(spec, hours, now)
        for record in hours.values():
            self._finalize(record)
        self._alerts(spec, hours, now)
        status = self._status(spec, hours, now)
        atomic_json(status_path(self.root), status)
        return status

    async def _harvest(self, spec: CampaignSpec, record: HourRecord) -> HourRecord:
        try:
            record = await self.harvester(spec, record, self.root)
        except Exception as error:  # boundary: one hour's failure never stops the others
            self.log.add(
                self.clock(),
                "harvest_error",
                "info",
                key=f"h{record.hour:02d}-{type(error).__name__}",
                hour=record.hour,
                error=type(error).__name__,
            )
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

    async def _reconcile_safely(
        self, spec: CampaignSpec, hours: dict[int, HourRecord], now: datetime
    ) -> None:
        """S3: a failed reconcile is alerted; harvests, reports and status go on."""
        self.reconciled_at = now
        try:
            await self.reconcile(spec, hours)
        except Exception as error:  # boundary: a failed reconcile is alerted; the loop goes on
            self.reconcile_failures += 1
            if self.reconcile_failures == 1:  # r1: keyed by the streak, so every outage alerts
                self.failing_since = now.strftime("%Y%m%dT%H%M")
            streak = self.failing_since
            self.log.add(
                now,
                "reconcile_error",
                "info",
                key=f"{streak}-{type(error).__name__}",
                error=type(error).__name__,
            )
            if self.reconcile_failures >= RECONCILE_HIGH_AFTER:
                self.log.add(
                    now,
                    "reconcile_error",
                    key=f"{streak}-persistent",
                    failures=self.reconcile_failures,
                )
            return
        self.reconcile_failures = 0

    def _alerts(self, spec: CampaignSpec, hours: dict[int, HourRecord], now: datetime) -> None:
        stamp = now.strftime("%Y%m%dT%H")
        for record in hours.values():
            for item in record.alerts:  # launch and harvest alerts, mirrored once each
                code = str(item.get("code") or item.get("kind") or "hour_alert")  # S2
                name = item.get("name")
                self.log.add(
                    now,
                    code,
                    str(item.get("level", "info")),
                    key=f"h{record.hour:02d}-{code}-{name or fingerprint(item)[:16]}",
                    hour=record.hour,
                    **({"name": name} if name else {}),
                )
            failed = sum(d.status == "create_failed" for d in record.droplets)
            if record.droplets and failed > CREATE_FAIL_SHARE * len(record.droplets):
                self.log.add(
                    now,
                    "create_failures",
                    key=f"h{record.hour:02d}",
                    hour=record.hour,
                    failed=failed,
                    total=len(record.droplets),
                )
        remaining = self._ratelimit()
        checks = {
            "disk_low": self._disk_free_pct() < DISK_FLOOR_PCT,
            "clock_unsynced": self._clock_unsynced(),
            "ratelimit_low": remaining is not None and remaining < RATELIMIT_FLOOR,
        }
        for code in [code for code, raised in checks.items() if raised]:
            self.log.add(now, code, key=f"{code}-{stamp}")

    def _clock_unsynced(self) -> bool:
        """Unsynced past the boot grace; an unknown uptime gets no grace."""
        if self.pacing.clock_synced() is not False:
            return False
        uptime = self.pacing.uptime()
        return uptime is None or uptime >= BOOT_GRACE_SECONDS

    def _disk_free_pct(self) -> float:
        usage = self.pacing.disk_usage(self.root)
        return round(100 * usage.free / usage.total, 1)

    def _ratelimit(self) -> int | None:
        return None if self.cloud is None else self.cloud.ratelimit_remaining

    def _status(self, spec: CampaignSpec, hours: dict[int, HourRecord], now: datetime) -> dict:
        rows = [
            {
                "hour": slot,
                "state": record.state,
                "launch_delay_seconds": record.launch_delay_seconds,
                "cells": dict(Counter(record.cells.values())),
                "live_workers": sum(map(live, record.droplets)),
            }
            for slot, record in sorted(hours.items())
        ]
        planned = [slot for slot, record in hours.items() if record.state == "planned"]
        workers = sum(row["live_workers"] for row in rows) + len(self.unreachable)
        return {
            "campaign_id": spec.campaign_id,
            "now": now.isoformat(),
            "next_launch_at": due_at(spec, min(planned)).isoformat() if planned else None,
            "hours": rows,
            "live_workers": workers,
            "alerts_open": sum(item.get("level") == "high" for item in read_alerts(self.root)),
            "traffic_latest": latest_summary(hours),
            "ratelimit_remaining": self._ratelimit(),
            "disk_free_pct": self._disk_free_pct(),
            "clock_synced": self.pacing.clock_synced(),
            "finished": workers == 0 and all(r.state in FINAL for r in hours.values()),
        }


def settled(status: dict) -> bool:
    """`--until-done`: every launched hour is final and no worker is left (the dry run)."""
    launched = [row for row in status["hours"] if row["state"] != "planned"]
    return (
        bool(launched)
        and status["live_workers"] == 0
        and all(row["state"] in FINAL for row in launched)
    )


async def run_collector(
    root: Path,
    *,
    harvester: Harvester,
    cloud: Cloud | None,
    remote_factory: RemoteFactory | None,
    clock: Clock = utc_now,
    until_done: bool = False,
    pacing: Pacing | None = None,
) -> dict:
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

    common = {"cloud": cloud, "remote_factory": remote_factory}  # 1c's own Clock by default
    return cloud, remote_factory, partial(launch_hour, **common), partial(harvest_hour, **common)


def _collector_command(args) -> int:
    root = Path(args.root)
    cloud, remote_factory, _, harvester = services(root, load_spec(root))

    async def main() -> dict:
        try:
            return await run_collector(
                root,
                harvester=harvester,
                cloud=cloud,
                remote_factory=remote_factory,
                until_done=args.until_done,
            )
        finally:
            await cloud.close()

    status = asyncio.run(main())
    print(json.dumps({"finished": status["finished"], "live_workers": status["live_workers"]}))
    return 0


def register(subcommands, handlers: dict) -> None:
    collector = subcommands.add_parser("collector", help="Harvest, finalize and reconcile (daemon)")
    collector.add_argument("--root", required=True)
    collector.add_argument(
        "--until-done", action="store_true", help="exit once every launched hour is final (dry run)"
    )
    handlers.update(collector=_collector_command)
