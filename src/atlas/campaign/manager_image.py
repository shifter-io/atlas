"""IMAGE-DAILY-1: the daily worker image timer on the Canada server's manager (off by default).

`atlas-image.timer` starts `campaign image-daily --apply` every day at 05:45 UTC, about 20
minutes after the orchestrator's DB-IP update (05:23). `orchestrator-enable` installs its unit
files with the others (host_layout.ASSETS). The owner rule of the hourly timers applies
(manager_daily): the timer has no [Install] section, so a reboot leaves it off. Only
`image-enable --apply` with the owner's recorded go checks the run's inputs (the dry run must
not refuse), writes the start time when `--at` is not 05:45 (a drop-in), adds the timer to
timers.target and starts it; `image-disable --apply` takes it out (a run under way finishes).
Production's host manager only: staging launches from production's image and never builds one.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import deploy, host_layout, manager_daily
from .contracts import PRODUCTION
from .image import Shell, run_shell

IMAGE_TIMER = "atlas-image.timer"
WANTS = f"/etc/systemd/system/timers.target.wants/{IMAGE_TIMER}"
DROP_IN = f"/etc/systemd/system/{IMAGE_TIMER}.d"
DEFAULT_AT = "05:45"
ACTIONS = ("image-enable", "image-disable")


def recorded(inventory_path: Path) -> dict[str, Any]:
    """The recorded manager, refused unless it is production's on the existing server."""
    inventory = manager_daily.recorded(inventory_path)
    layout = host_layout.of(inventory)
    if layout is None or layout.environment != PRODUCTION:
        raise deploy.DeployError(
            "the daily image runs only on production's host manager (staging uses its image)"
        )
    return inventory


def at_lines(at: str) -> list[str]:
    """Shell that sets the timer's start (UTC): the unit's own 05:45, else a drop-in."""
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", at):
        raise deploy.DeployError("--at must be HH:MM (UTC)")
    if at == DEFAULT_AT:
        return [f"rm -f {DROP_IN}/at.conf"]
    calendar = f"*-*-* {at}:00 UTC"
    return [
        f"mkdir -p {DROP_IN}",
        f"printf '[Timer]\\nOnCalendar=\\nOnCalendar={calendar}\\n' > {DROP_IN}/at.conf",
    ]


def enable_lines(at: str) -> list[str]:
    return [
        "set -eu",
        f"systemctl cat {IMAGE_TIMER} > /dev/null",
        # exit 3: the run would refuse (no release, no current wheel, no requirements, ...)
        f"{manager_daily.ATLAS} campaign image-daily --root {deploy.BASE} > /dev/null",
        *at_lines(at),
        "systemctl daemon-reload",
        f"systemctl add-wants timers.target {IMAGE_TIMER}",
        f"systemctl start {IMAGE_TIMER}",
        f"systemctl is-active {IMAGE_TIMER}",
    ]


def off_lines() -> list[str]:
    """Shell that takes the timer out of timers.target and stops it."""
    return [
        f"systemctl stop {IMAGE_TIMER} || true",  # not installed yet: nothing to stop
        f"rm -f {WANTS}",
        "systemctl daemon-reload",
    ]


async def image_enable(
    inventory_path: Path,
    *,
    apply: bool = False,
    owner_go: Path | None = None,
    at: str = DEFAULT_AT,
    shell: Shell = run_shell,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Turn the daily image rebuild on, only with the owner's recorded go."""
    inventory = recorded(inventory_path)
    if not inventory.get("orchestrator"):
        raise deploy.DeployError("run orchestrator-enable --apply first")
    lines = enable_lines(at)  # checks --at before anything else
    result = {"applied": apply, "timer": host_layout.script(inventory, IMAGE_TIMER), "at": at}
    if not apply:
        return result
    go = manager_daily.go_record(owner_go, now or datetime.now(UTC))
    out = await deploy.Manager(inventory, inventory_path, shell).run(
        host_layout.script(inventory, "\n".join(lines)), "image-enable"
    )
    if out.strip().splitlines()[-1:] != ["active"]:
        raise deploy.DeployError("the image timer is not active")
    deploy.save(inventory_path, inventory, image_daily=go | {"on": True, "at_utc": at})
    return result


async def image_disable(
    inventory_path: Path, *, apply: bool = False, shell: Shell = run_shell, now=None
) -> dict[str, Any]:
    """Turn the daily image rebuild off (it stays off after a reboot)."""
    inventory = recorded(inventory_path)
    result = {"applied": apply, "timer": host_layout.script(inventory, IMAGE_TIMER)}
    if not apply:
        return result
    script = ["set -eu", *off_lines(), f"systemctl is-active {IMAGE_TIMER} || true"]
    out = await deploy.Manager(inventory, inventory_path, shell).run(
        host_layout.script(inventory, "\n".join(script)), "image-disable"
    )
    if out.strip().splitlines()[-1:] != ["inactive"]:
        raise deploy.DeployError("the image timer did not stop")
    stamp = (now or datetime.now(UTC)).isoformat()
    deploy.save(inventory_path, inventory, image_daily={"on": False, "at": stamp})
    return result
