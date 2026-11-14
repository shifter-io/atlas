"""PULSE-ONDEMAND-1: the on-demand pulses' hourly timer on the manager (off by default).

`atlas-pulse.timer` runs at HH:57 and starts the next hour's `atlas daily pulse-hour` as
its own unit `atlas-pulse-YYYYMMDDTHH`, on the pulse root PULSE_ROOT (its `fleet.json`,
written by the operator, as the pulse fleet's) and the daily test's installed formulas.

The owner rule of the daily timer applies (manager_daily): the unit file has no [Install]
section and `orchestrator-enable` installs it without enabling or starting it, so a reboot
leaves it off. Only `pulse-enable --apply` with the owner's recorded go checks the pulse root
and the hour's droplets against the cap (`daily pulse-plan`), switches the orchestrator to
on-demand pulses (`daily pulse-switch on`: `pulse.on_demand` on, the HTTP targets to 60
minutes), then adds the timer to timers.target and starts it. `pulse-disable --apply` takes the
timer out (an hour running finishes and removes its droplets); `pulse.on_demand` stays on, so
the minute planners never plan for the on-demand nodes left in the nodes table.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import deploy, host_layout, manager_daily
from .image import Shell, run_shell

PULSE_SERVICE, PULSE_TIMER = "atlas-pulse.service", "atlas-pulse.timer"
PULSE_RUNS = PULSE_TIMER.removesuffix(".timer") + "-*"  # each hour's transient unit
PULSE_ROOT = "/opt/atlas-campaign/pulse"
WANTS = f"/etc/systemd/system/timers.target.wants/{PULSE_TIMER}"
ACTIONS = ("pulse-enable", "pulse-disable")
SWITCH = "pulse-switch "  # the prefix of `daily pulse-switch`'s JSON line


def off_lines() -> list[str]:
    """Shell that takes the pulse timer out of timers.target and stops it."""
    return [
        f"systemctl stop {PULSE_TIMER} || true",  # not installed yet: nothing to stop
        f"rm -f {WANTS}",
        "systemctl daemon-reload",
    ]


def enable_lines() -> list[str]:
    atlas, database = manager_daily.ATLAS, deploy.DATABASE_FILE
    return [
        "set -eu",
        f"systemctl cat {PULSE_TIMER} > /dev/null",
        f"test -f {PULSE_ROOT}/fleet.json",
        f"{atlas} daily pulse-plan --formulas {manager_daily.FORMULAS} > /dev/null",  # exit 3: cap
        # An assignment, so set -e stops on a failed switch (inside echo's argument it would not).
        f"switched=$({atlas} daily pulse-switch on --database-file {database})",
        f'echo "{SWITCH}$switched"',
        f"systemctl add-wants timers.target {PULSE_TIMER}",
        f"systemctl start {PULSE_TIMER}",
        f"systemctl is-active {PULSE_TIMER}",
    ]


async def pulse_enable(
    inventory_path: Path,
    *,
    apply: bool = False,
    owner_go: Path | None = None,
    shell: Shell = run_shell,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Turn the hourly on-demand pulses on, only with the owner's recorded go."""
    inventory = manager_daily.recorded(inventory_path)
    if not inventory.get("orchestrator"):
        raise deploy.DeployError("run orchestrator-enable --apply first")
    result = {"applied": apply, "timer": host_layout.script(inventory, PULSE_TIMER)}
    if not apply:
        return result
    go = manager_daily.go_record(owner_go, now or datetime.now(UTC))
    remote = deploy.Manager(inventory, inventory_path, shell)
    script = host_layout.script(inventory, "\n".join(enable_lines()))
    code, out = await remote.shell(remote.argv(script), b"")
    lines = out.strip().splitlines()
    if code != 0:
        raise deploy.DeployError(f"manager step failed: pulse-enable (exit {code})")
    if lines[-1:] != ["active"]:
        raise deploy.DeployError("the pulse timer is not active")
    switched = [line.removeprefix(SWITCH) for line in lines if line.startswith(SWITCH)]
    deploy.save(inventory_path, inventory, pulses=go | {"on": True})
    return result | {"switch": switched[-1] if switched else None}


async def pulse_disable(
    inventory_path: Path, *, apply: bool = False, shell: Shell = run_shell, now=None
) -> dict[str, Any]:
    """Turn the hourly on-demand pulses off (they stay off after a reboot)."""
    inventory = manager_daily.recorded(inventory_path)
    result = {"applied": apply, "timer": host_layout.script(inventory, PULSE_TIMER)}
    if not apply:
        return result
    script = ["set -eu", *off_lines(), f"systemctl is-active {PULSE_TIMER} || true"]
    out = await deploy.Manager(inventory, inventory_path, shell).run(
        host_layout.script(inventory, "\n".join(script)), "pulse-disable"
    )
    if out.strip().splitlines()[-1:] != ["inactive"]:
        raise deploy.DeployError("the pulse timer did not stop")
    stamp = (now or datetime.now(UTC)).isoformat()
    deploy.save(inventory_path, inventory, pulses={"on": False, "at": stamp})
    return result
