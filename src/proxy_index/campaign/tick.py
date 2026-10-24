"""The launcher tick, the missed-slot watchdog and network pause, over the hour ledger.

Every slot launches at `starts_at + k * slot_minutes - create_lead_seconds` (HH:58:30 for the
default hourly slots), whatever earlier slots are doing (D6). A late tick launches every due slot
at once; no slot is skipped or relaunched. `timer_calendars` gives the systemd times that match.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..onboarding.files import atomic_json, private_dir, read_json
from .contracts import (
    CampaignSpec,
    HourLauncher,
    HourRecord,
    alerts_path,
    hour_path,
    spec_path,
)

LATE_SECONDS = 60.0
DUE_TOLERANCE = timedelta(seconds=2)  # the timer may fire a fraction of a second early
LAUNCHABLE = ("planned", "launching")  # `launching` is a crashed tick: resume, never re-create
FINAL = ("done", "failed")
Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def load_spec(root: Path) -> CampaignSpec:
    return CampaignSpec.from_json(spec_path(root).read_text())


def slot_length(spec: CampaignSpec) -> timedelta:
    return timedelta(minutes=spec.slot_minutes)


def scheduled_at(spec: CampaignSpec, slot: int) -> datetime:
    return datetime.fromisoformat(spec.starts_at) + slot * slot_length(spec)


def slot_label(spec: CampaignSpec, slot: int, short: bool = False) -> str:
    """`hour NN` (`hNN`) for hourly slots, else `slot NN (HH:MM UTC)` (`sNN HH:MM`)."""
    if spec.slot_minutes == 60:
        return f"h{slot:02d}" if short else f"hour {slot:02d}"
    at = scheduled_at(spec, slot).astimezone(UTC)
    return f"s{slot:02d} {at:%H:%M}" if short else f"slot {slot:02d} ({at:%H:%M} UTC)"


def due_at(spec: CampaignSpec, slot: int) -> datetime:
    return scheduled_at(spec, slot) - timedelta(seconds=spec.create_lead_seconds)


def load_hour(root: Path, spec: CampaignSpec, slot: int) -> HourRecord:
    path = hour_path(root, slot)
    return (HourRecord.from_dict(read_json(path)) if path.exists()
            else HourRecord(spec.campaign_id, slot, scheduled_at(spec, slot).isoformat()))


def load_hours(root: Path, spec: CampaignSpec) -> dict[int, HourRecord]:
    """Every slot's record; slots never launched come back `planned`."""
    return {slot: load_hour(root, spec, slot) for slot in range(spec.hours)}


def save_hour(root: Path, record: HourRecord) -> None:
    atomic_json(hour_path(root, record.hour), record.to_dict())


def read_alerts(root: Path) -> list[dict]:
    path = alerts_path(root)
    lines = path.read_text().splitlines() if path.exists() else []
    return [json.loads(line) for line in lines if line.strip()]


class AlertLog:
    """alerts.jsonl and its raised (code, key) pairs, owned by one caller (r3); the pairs are
    re-read only when the append-only file has grown (m3)."""

    def __init__(self, root: Path):
        self.root, self.path, self.size, self.raised = root, alerts_path(root), -1, set()

    def _pairs(self) -> set[tuple[str, str]]:
        size = self.path.stat().st_size if self.path.exists() else 0
        if size != self.size:
            self.size, self.raised = size, {(str(item.get("code")), str(item.get("key")))
                                            for item in read_alerts(self.root)}
        return self.raised

    def add(self, now: datetime, code: str, level: str = "high", key: str | None = None,
            **details: object) -> bool:
        """Append one alert unless (code, key) was raised before. No IPs."""
        key = key or code
        if (code, key) in self._pairs():
            return False
        entry = {"time": now.isoformat(), "code": code, "level": level, "key": key, **details}
        private_dir(self.path.parent)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (json.dumps(entry, sort_keys=True, allow_nan=False) + "\n").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        self.raised.add((code, key))
        self.size = self.path.stat().st_size
        return True


def alert(root: Path, now: datetime, code: str, level: str = "high", key: str | None = None,
          **details: object) -> bool:
    """One alert from a short-lived caller (tick, watchdog)."""
    return AlertLog(root).add(now, code, level, key, **details)


def overlay_path(root: Path) -> Path:
    return root / "network-overlay.json"


def paused_networks(root: Path) -> list[str]:
    path = overlay_path(root)
    return sorted(read_json(path).get("paused", [])) if path.exists() else []


def set_paused(root: Path, network: str, paused: bool) -> list[str]:
    """Owner request only: later hours skip (or again launch) `network`; running rounds go on."""
    if network not in load_spec(root).networks:
        raise ValueError(f"{network!r} is not a network of this campaign")
    names = set(paused_networks(root)) - {network} | ({network} if paused else set())
    atomic_json(overlay_path(root), {"paused": sorted(names), "updated_at": utc_now().isoformat()})
    return sorted(names)


def active_spec(spec: CampaignSpec, paused: list[str]) -> CampaignSpec:
    networks = tuple(net for net in spec.networks if net not in paused)
    return replace(spec, networks=networks,
                   manifests={net: spec.manifests[net] for net in networks})


def timer_calendars(slot_minutes: int, lead_seconds: int) -> dict[str, str]:
    """OnCalendar per timer: the tick `lead_seconds` before each slot, the watchdog's `armed`
    check 30 s and its `fired` check 120 s after it (hourly: *:58:30, *:00:30, *:02:00)."""
    def calendar(offset: int) -> str:
        moments = sorted((start * 60 + offset) % 3600 for start in range(0, 60, slot_minutes))
        minutes = ",".join(f"{m // 60:02d}" for m in moments)
        return f"*-*-* *:{minutes}:{moments[0] % 60:02d}"
    return {"tick": calendar(-lead_seconds), "watchdog-armed": calendar(30),
            "watchdog-fired": calendar(120)}


def _systemctl(*args: str) -> None:
    subprocess.run(["systemctl", *args], check=True, capture_output=True, timeout=30)


def _due_slots(root: Path, spec: CampaignSpec, now: datetime, slot: int | None,
               now_mode: bool) -> list[int]:
    if now_mode:
        if slot is None or not 0 <= slot < spec.hours:
            raise ValueError("--now needs a slot of this campaign")
        return [slot] if load_hour(root, spec, slot).state in LAUNCHABLE else []
    return [k for k in range(spec.hours) if due_at(spec, k) <= now + DUE_TOLERANCE
            and load_hour(root, spec, k).state in LAUNCHABLE]


async def _launch(root: Path, spec: CampaignSpec, slot: int, launcher: HourLauncher,
                  clock: Clock, now_mode: bool) -> None:
    record = load_hour(root, spec, slot)
    paused = paused_networks(root)
    if record.state == "planned":
        now = clock()
        delay = max(0.0, (now - due_at(spec, slot)).total_seconds())
        record.fired_at, record.launch_delay_seconds = now.isoformat(), round(delay, 3)
        if paused:
            record.alerts.append({"code": "network_paused", "level": "info", "networks": paused})
        record.state = "launching"
        save_hour(root, record)
        if delay > LATE_SECONDS and not now_mode:
            alert(root, now, "late_launch", key=f"h{slot:02d}", hour=slot,
                  launch_delay_seconds=record.launch_delay_seconds)
    launch_spec = active_spec(spec, paused)
    if not launch_spec.networks:
        record.state, record.finished_at = "done", clock().isoformat()
        save_hour(root, record)
        return
    record = await launcher(launch_spec, record, root)
    # m1: the launcher saves its own record; once it is past `launching` the collector owns it.
    if load_hour(root, spec, slot).state in LAUNCHABLE:
        record.launched_at = record.launched_at or clock().isoformat()
        save_hour(root, record)


def _disable(spec: CampaignSpec, root: Path, disable_timer: Callable[[], None] | None) -> None:
    """S7: the timer goes only when no slot is left `planned` or `launching`."""
    if any(load_hour(root, spec, k).state in LAUNCHABLE for k in range(spec.hours)):
        return
    unit = f"proxy-index-campaign-tick@{spec.campaign_id}.timer"
    try:
        (disable_timer or (lambda: _systemctl("disable", "--now", unit)))()
    except (OSError, subprocess.SubprocessError) as error:
        alert(root, utc_now(), "timer_disable_failed", "info", error=type(error).__name__)


async def run_tick(root: Path, *, launcher: HourLauncher, clock: Clock = utc_now,
                   now_mode: bool = False, slot: int | None = None,
                   disable_timer: Callable[[], None] | None = None) -> list[int]:
    """Launch every due slot still `planned` (or resume a crashed `launching` one), at once,
    under `flock(ledger/.lock)`; `now_mode` launches only `slot`, immediately. Returns them."""
    lock = root / "ledger" / ".lock"
    private_dir(lock.parent)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        await asyncio.to_thread(fcntl.flock, fd, fcntl.LOCK_EX)
        spec = load_spec(root)
        slots = _due_slots(root, spec, clock(), slot, now_mode)
        results = await asyncio.gather(
            *(_launch(root, spec, k, launcher, clock, now_mode) for k in slots),
            return_exceptions=True)
        if not now_mode:
            _disable(spec, root, disable_timer)
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            raise failures[0]  # the service restarts and resumes the `launching` record
        return slots
    finally:
        os.close(fd)


def run_watchdog(root: Path, *, check: str, clock: Clock = utc_now,
                 start_tick: Callable[[], None] | None = None,
                 timer_enabled: Callable[[], bool] | None = None) -> str | None:
    """30 s after a slot starts (`armed`, *:00:30 hourly) its workers are armed; 120 s after
    (`fired`, *:02:00) it fired. An enabled timer also gets the tick started; a disabled one
    (the operator's) only the alarm (S7)."""
    spec, now = load_spec(root), clock()
    slot = (now - scheduled_at(spec, 0)) // slot_length(spec)
    if not 0 <= slot < spec.hours:
        return None
    unit = f"proxy-index-campaign-tick@{spec.campaign_id}"
    enabled = timer_enabled or (lambda: subprocess.run(
        ["systemctl", "is-enabled", f"{unit}.timer"], capture_output=True, text=True,
        timeout=30).stdout.strip() == "enabled")
    record = load_hour(root, spec, slot)
    if _on_time(record, check):
        return None
    timer = enabled()
    if not timer and record.state != "planned":
        return None
    alert(root, now, "missed_launch", key=f"h{slot:02d}-{check}", hour=slot, check=check,
          timer="enabled" if timer else "disabled")
    if timer:
        (start_tick or (lambda: _systemctl("start", "--no-block", f"{unit}.service")))()
    return "missed_launch"


def _on_time(record: HourRecord, check: str) -> bool:
    """S1: an hour still `launching` is on time once any of its workers is armed."""
    if check == "fired":
        return record.fired_at is not None
    armed = any(d.status in ("armed", "running") for d in record.droplets)
    return record.state in ("running", "harvesting", *FINAL) or (
        record.state == "launching" and record.fired_at is not None and armed)


def _tick_command(args) -> int:
    from .collector import services

    root = Path(args.root)
    cloud, _, launcher, _ = services(root, load_spec(root))

    async def main() -> list[int]:
        try:
            return await run_tick(root, launcher=launcher, now_mode=args.now, slot=args.slot)
        finally:
            await cloud.close()

    slots = asyncio.run(main())
    print(json.dumps({"launched": slots}))
    return 0


def _watchdog_command(args) -> int:
    print(json.dumps({"alert": run_watchdog(Path(args.root), check=args.check)}))
    return 0


def _network_command(args) -> int:
    paused = set_paused(Path(args.root), args.network, args.action == "pause")
    print(json.dumps({"paused": paused}))
    return 0


def register(subcommands, handlers: dict) -> None:
    tick = subcommands.add_parser("tick", help="Launch every due slot (systemd timer, *:58:30)")
    tick.add_argument("--root", required=True)
    tick.add_argument("--slot", type=int)
    tick.add_argument("--now", action="store_true", help="launch --slot immediately (dry run)")
    watchdog = subcommands.add_parser("watchdog", help="Missed-slot alarm (*:00:30, *:02:00)")
    watchdog.add_argument("--root", required=True)
    watchdog.add_argument("--check", choices=("armed", "fired"), required=True)
    network = subcommands.add_parser("network", help="Pause or resume a network from next hour")
    network.add_argument("action", choices=("pause", "resume"))
    network.add_argument("network")
    network.add_argument("--root", required=True)
    handlers.update(tick=_tick_command, watchdog=_watchdog_command, network=_network_command)
