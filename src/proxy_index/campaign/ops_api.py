"""Campaign cloud operations: SSH keys, firewalls, the worker snapshot and builder projects.

Every call goes through an allowlisted `DigitalOcean` method. A key, firewall, snapshot or
droplet is removed only by `scope.guarded_delete`, with its recorded id and a `pi-camp-` name.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .contracts import WORKER_TAG, Droplet, DropletRecord, Scope
from .do_api import DigitalOcean
from .scope import Kind, Ledger, Role, assign_and_verify_project, guarded_delete

Sleep = Callable[[float], Awaitable[None]]
POLL_SECONDS = 5.0
ACTION_LIMIT_SECONDS = 2 * 3600.0  # a snapshot or a transfer of about 3 GB
READY_LIMIT_SECONDS = 600.0
# The builder's and manager's `created_at` come from DigitalOcean's clock, not this Mac's.
CLOCK_SKEW = timedelta(minutes=10)
EVERYWHERE = {"addresses": ["0.0.0.0/0", "::/0"]}


class OpsError(RuntimeError):
    """A cloud operation that did not reach its expected state."""


def ops_scope(project_id: str, since: datetime, manager_id: str = "") -> Scope:
    """The scope of a builder or manager: no campaign; `since` is its inventory time."""
    return Scope("", "", project_id, manager_id, since - CLOCK_SKEW)


async def ensure_ssh_key(cloud: DigitalOcean, name: str, public_key: str) -> tuple[str, bool]:
    """(key id, created now): reuse our registered key with this public part, or add it."""
    wanted = public_key.split()[:2]
    for key in await cloud.ssh_keys():
        if str(key.get("public_key", "")).split()[:2] == wanted:
            return str(key["id"]), False
    return str((await cloud.create_ssh_key(name, " ".join(wanted)))["id"]), True


async def assign(cloud: DigitalOcean, scope: Scope, droplet: Droplet, audit: Path,
                 role: Role) -> None:
    """Move a builder or manager we just created into project Proxy Index, or fail closed."""
    records = {droplet.id: DropletRecord("", droplet.region, 0, droplet.name, droplet.id)}
    _, failed = await assign_and_verify_project(cloud, scope, [droplet.id], records, audit, role)
    if failed:
        raise OpsError(f"{role} {droplet.id} not confirmed in the project "
                       f"(record {records[droplet.id].status})")


async def release(cloud: DigitalOcean, kind: Kind, resource_id: str, scope: Scope, audit: Path,
                  role: Role = "builder") -> str:
    """Remove one recorded resource through the scope guard ("deleted" or "absent").

    The ledger holds only this id. A firewall that names droplets (the manager's) is refused
    here; teardown passes `guarded_delete` a ledger that also records those droplet ids."""
    return await guarded_delete(cloud, kind, resource_id, scope,
                                Ledger({kind: {str(resource_id)}}), audit, role)


async def _poll(check: Callable[[], Awaitable[Any]], what: str, limit: float,
                sleep: Sleep) -> Any:
    for _ in range(max(1, int(limit / POLL_SECONDS))):
        found = await check()
        if found:
            return found
        await sleep(POLL_SECONDS)
    raise OpsError(f"timed out waiting for {what}")


async def wait_action(cloud: DigitalOcean, action: dict, sleep: Sleep = asyncio.sleep,
                      limit: float = ACTION_LIMIT_SECONDS) -> None:
    async def done() -> bool:
        status = (await cloud.action(action["id"]))["status"]
        if status == "errored":
            raise OpsError(f"action {action['id']} ({action.get('type')}) errored")
        return status == "completed"

    await _poll(done, f"action {action['id']}", limit, sleep)


async def wait_droplet(cloud: DigitalOcean, droplet_id: str, status: str,
                       sleep: Sleep = asyncio.sleep,
                       limit: float = READY_LIMIT_SECONDS) -> Droplet:
    """The droplet once it reaches `status` (and, when active, has a public IPv4)."""
    async def reached() -> Droplet | None:
        found = await cloud.get(droplet_id)
        if found is None:
            raise OpsError(f"droplet {droplet_id} is gone")
        ready = found.status == status and (status != "active" or found.public_ipv4)
        return found if ready else None

    return await _poll(reached, f"droplet {droplet_id} {status}", limit, sleep)


async def snapshot(cloud: DigitalOcean, scope: Scope, builder_id: str, name: str,
                   sleep: Sleep = asyncio.sleep) -> dict[str, Any]:
    """Shut the builder down, snapshot it and return the snapshot image."""
    project = scope.project_id
    await wait_action(cloud, await cloud.droplet_action(builder_id, "shutdown", project), sleep)
    await wait_droplet(cloud, builder_id, "off", sleep)
    await wait_action(cloud, await cloud.droplet_action(builder_id, "snapshot", project, name),
                      sleep)
    found = [s for s in await cloud.droplet_snapshots(builder_id) if s.get("name") == name]
    if len(found) != 1:
        raise OpsError(f"expected one snapshot named {name}, found {len(found)}")
    return found[0]


async def transfer(cloud: DigitalOcean, image_id: str, regions: list[str],
                   sleep: Sleep = asyncio.sleep) -> list[str]:
    """Copy the snapshot, one region after another, to every region it is not in yet;
    returns the regions it is in."""
    image = await cloud.image(image_id) or {}
    for region in [r for r in regions if r not in image.get("regions", [])]:
        await wait_action(cloud, await cloud.transfer_image(image_id, region), sleep)
    present = (await cloud.image(image_id) or {}).get("regions", [])
    if set(regions) - set(present):
        raise OpsError(f"snapshot {image_id} missing in {sorted(set(regions) - set(present))}")
    return sorted(present)


def ssh_from(address: str) -> dict[str, Any]:
    return {"protocol": "tcp", "ports": "22", "sources": {"addresses": [f"{address}/32"]}}


def outbound_all() -> list[dict[str, Any]]:
    return [{"protocol": "tcp", "ports": "all", "destinations": EVERYWHERE},
            {"protocol": "udp", "ports": "all", "destinations": EVERYWHERE},
            {"protocol": "icmp", "destinations": EVERYWHERE}]


def worker_firewall(manager_ipv4: str) -> dict[str, Any]:
    """Workers (by tag): SSH only from the manager; all outbound."""
    return {"name": "pi-camp-worker-fw", "inbound_rules": [ssh_from(manager_ipv4)],
            "outbound_rules": outbound_all(), "tags": [WORKER_TAG], "droplet_ids": []}


def manager_firewall(mac_ipv4: str, manager_id: str) -> dict[str, Any]:
    """The manager (by droplet id): SSH only from the Mac's public IPv4; all outbound."""
    return {"name": "pi-camp-manager-fw", "inbound_rules": [ssh_from(mac_ipv4)],
            "outbound_rules": outbound_all(), "tags": [], "droplet_ids": [int(manager_id)]}


async def apply_firewall(cloud: DigitalOcean, scope: Scope, rules: dict[str, Any],
                         firewall_id: str | None = None) -> str:
    """Create the firewall, or replace the recorded one; returns its id. Its droplets must be
    the campaign's and in the scope's project (checked by the client on a fresh GET)."""
    if firewall_id is None:
        return str((await cloud.create_firewall(rules, scope.project_id))["id"])
    return str((await cloud.update_firewall(firewall_id, rules, scope.project_id))["id"])

