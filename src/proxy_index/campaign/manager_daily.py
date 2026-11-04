"""MANAGER-ORCH-1: the daily test's inputs on the manager, and its hourly timer (off by default).

The manager holds its own copy of the repository's formulas (`formulas/<network>.json`: each
country's test hour per network) under DAILY_DIR/formulas, and a link DAILY_DIR/template to the
template campaign (a planned campaign uploaded with `manager upload-spec`, recorded in the
inventory as `daily_template`). `orchestrator-enable` installs both, checks them on the manager
for all 24 hours (`proxy-index daily check`) and compares every formula's SHA-256 with the copy
sent; nothing is passed to the hourly job by an operator.

Owner rule (2026-11-04): the hourly timer never starts on its own. Its unit file has no
[Install] section, `orchestrator-enable` and `deploy` never enable or start it, so a reboot
leaves it off. Only `hourly-enable --apply`, with the owner's recorded go, adds it to
timers.target (`systemctl add-wants`) and starts it; `hourly-disable --apply` takes it out.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import deploy
from .contracts import short_id
from .image import Shell, run_shell

DAILY_SERVICE, DAILY_TIMER = "proxy-index-daily.service", "proxy-index-daily.timer"
DAILY_DIR = "/opt/proxy-index-campaign/daily"
FORMULAS, TEMPLATE = f"{DAILY_DIR}/formulas", f"{DAILY_DIR}/template"
CAMPAIGNS = "/opt/proxy-index-campaign/campaigns"
PROXY_INDEX = "/opt/proxy-index-campaign/venv/bin/proxy-index"
WANTS = f"/etc/systemd/system/timers.target.wants/{DAILY_TIMER}"
ACTIONS = ("hourly-enable", "hourly-disable")
ACCOUNT = "proxy-index"  # the brief: the "Proxy Index" team account, never leftclick


def recorded(inventory_path: Path) -> dict[str, Any]:
    """The recorded manager, refused unless it is the proxy-index account's (before any call)."""
    inventory = deploy.load_inventory(inventory_path)
    if not inventory.get("manager_id") or not inventory.get("host"):
        raise deploy.DeployError("no manager is recorded")
    if inventory.get("account") != ACCOUNT:
        raise deploy.DeployError(
            f"this inventory's manager is on account {inventory.get('account')}; these steps act "
            f"only on the {ACCOUNT} account's manager: pass its inventory with --inventory"
        )
    return inventory


def go_record(owner_go: Path | None, now: datetime) -> dict[str, str]:
    """The owner's go for a step (D13, hourly tests): the answer file's path, SHA-256 and time."""
    if owner_go is None or not owner_go.is_file() or not owner_go.read_text().strip():
        raise deploy.DeployError("--apply needs --owner-go with the owner's recorded answer")
    digest = hashlib.sha256(owner_go.read_bytes()).hexdigest()
    return {"owner_go": str(owner_go.resolve()), "owner_go_sha256": digest, "at": now.isoformat()}


def formula_files(directory: Path) -> dict[str, bytes]:
    """The repository's formula JSONs, checked locally first (`daily.planner.load_schedules`)."""
    from ..daily.planner import load_schedules

    try:
        if not load_schedules(directory):
            raise ValueError("no formula")
    except (OSError, ValueError) as exc:
        raise deploy.DeployError(f"formulas in {directory}: {exc}") from None
    return {path.name: path.read_bytes() for path in sorted(directory.glob("*.json"))}


def digests(found: dict[str, bytes]) -> dict[str, str]:
    return {name: hashlib.sha256(data).hexdigest() for name, data in found.items()}


def template_id(inventory: dict[str, Any], wanted: str | None) -> str:
    """The daily template: `wanted`, else the recorded one; an uploaded campaign's id."""
    found = wanted or inventory.get("daily_template")
    if not found:
        raise deploy.DeployError("name the daily template once: --daily-template <campaign id>")
    try:
        short_id(found)
    except ValueError:
        raise deploy.DeployError("the daily template must be a campaign id") from None
    if found not in inventory.get("campaigns", []):
        raise deploy.DeployError(f"campaign {found} was never uploaded to the recorded manager")
    return found


def check_lines(source: str, template: str) -> list[str]:
    """Shell that prints `daily-check <JSON>` and stops (exit 4) unless the check is ok."""
    return [
        f'check=$({PROXY_INDEX} daily check --formulas "{source}" --template "{template}")',
        'echo "daily-check $check"',
        'case "$check" in \'{"ok":true,\'*) ;; *) exit 4 ;; esac',
    ]


def install_lines(cid: str) -> list[str]:
    """Shell run with the staged tar in "$stage": check the staged formulas against the template,
    then swap them in, point the template link, and print each installed formula's SHA-256."""
    template = f"{CAMPAIGNS}/{cid}"
    return [
        f"test -f {template}/campaign-spec.json",
        *check_lines("$stage/formulas", template),
        f"install -d -m 700 {DAILY_DIR}",
        f"rm -rf {FORMULAS}.new {FORMULAS}.old",
        f'cp -R "$stage/formulas" {FORMULAS}.new',
        f"if [ -d {FORMULAS} ]; then mv {FORMULAS} {FORMULAS}.old; fi",
        f"mv {FORMULAS}.new {FORMULAS} && rm -rf {FORMULAS}.old",
        f"ln -sfn {template} {TEMPLATE}.new && mv -Tf {TEMPLATE}.new {TEMPLATE}",
        f"(cd {FORMULAS} && sha256sum -- *.json) | sed 's/^/formula /'",
    ]


def verify(out: str, code: int, sent: dict[str, str], what: str) -> dict[str, Any]:
    """The `daily-check` JSON from a script's output, after its exit code and, when `sent` is
    given, the installed formulas' SHA-256 (`formula <sha> <name>` lines) against the sent."""
    lines = out.strip().splitlines()
    found = [line.removeprefix("daily-check ") for line in lines if line.startswith("daily-check ")]
    check = json.loads(found[-1]) if found else None
    if check is not None and not check.get("ok"):
        raise deploy.DeployError(f"the daily check failed: {'; '.join(check['issues'][:5])}")
    if code != 0 or check is None:
        raise deploy.DeployError(f"manager step failed: {what} (exit {code})")
    installed = {
        parts[2]: parts[1]
        for parts in (line.split() for line in lines if line.startswith("formula "))
        if len(parts) == 3
    }
    if sent and installed != sent:
        raise deploy.DeployError("the formulas on the manager differ from the ones sent")
    return check


def off_lines() -> list[str]:
    """Shell that takes the hourly timer out of timers.target and stops it (a running hour
    finishes and cleans up on its own)."""
    return [
        f"systemctl stop {DAILY_TIMER} || true",  # not installed yet: nothing to stop
        f"rm -f {WANTS}",
        "systemctl daemon-reload",
    ]


async def hourly_enable(
    inventory_path: Path,
    *,
    apply: bool = False,
    owner_go: Path | None = None,
    shell: Shell = run_shell,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Turn the hourly daily test on, only with the owner's recorded go (owner rule)."""
    inventory = recorded(inventory_path)
    if not inventory.get("orchestrator") or not inventory.get("daily_template"):
        raise deploy.DeployError("run orchestrator-enable --apply first")
    result = {"applied": apply, "timer": DAILY_TIMER, "template": inventory["daily_template"]}
    if not apply:
        return result
    go = go_record(owner_go, now or datetime.now(UTC))
    script = [
        "set -eu",
        f"systemctl cat {DAILY_TIMER} > /dev/null",
        *check_lines(FORMULAS, TEMPLATE),
        f"systemctl add-wants timers.target {DAILY_TIMER}",
        f"systemctl start {DAILY_TIMER}",
        f"systemctl is-active {DAILY_TIMER}",
    ]
    remote = deploy.Manager(inventory, inventory_path, shell)
    code, out = await remote.shell(remote.argv("\n".join(script)), b"")
    check = verify(out, code, {}, "hourly-enable")
    if out.strip().splitlines()[-1:] != ["active"]:
        raise deploy.DeployError("the hourly timer is not active")
    deploy.save(inventory_path, inventory, hourly=go | {"on": True})
    return result | {"droplets_by_hour": check["droplets_by_hour"]}


async def hourly_disable(
    inventory_path: Path, *, apply: bool = False, shell: Shell = run_shell, now=None
) -> dict[str, Any]:
    """Turn the hourly daily test off (it stays off after a reboot)."""
    inventory = recorded(inventory_path)
    result = {"applied": apply, "timer": DAILY_TIMER}
    if not apply:
        return result
    script = ["set -eu", *off_lines(), f"systemctl is-active {DAILY_TIMER} || true"]
    out = await deploy.Manager(inventory, inventory_path, shell).run(
        "\n".join(script), "hourly-disable"
    )
    if out.strip().splitlines()[-1:] != ["inactive"]:
        raise deploy.DeployError("the hourly timer did not stop")
    stamp = (now or datetime.now(UTC)).isoformat()
    deploy.save(inventory_path, inventory, hourly={"on": False, "at": stamp})
    return result
