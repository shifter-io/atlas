"""BP-FIREWALL: the browser hosts' public IPv4s as sources of the manager's node API rule.

`campaign manager browser-hosts --ip A[,B...] [--apply]` (or `--clear`) sets exactly those
addresses, as /32 sources beside the pulse-node tag, and records them in the inventory
(`browser_host_ipv4s`). Every later firewall rebuild (`deploy`, `allow-me`,
`orchestrator-enable`) reads them there through `manager_rules`, so it keeps them. Without
--apply it only prints the node API rule before and after.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any

from . import deploy, manager_daily, ops_api
from .contracts import PULSE_TAG
from .do_api import DigitalOcean
from .manager_rules import BROWSER_HOSTS, manager_rules

MAX_HOSTS = 8
DOCUMENTATION = tuple(
    ipaddress.IPv4Network(block) for block in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
)


def _refusal(address: ipaddress.IPv4Address) -> str | None:
    """Why `address` is not a public IPv4, or None."""
    if address.is_loopback:
        return "a loopback address"
    if any(address in block for block in DOCUMENTATION):
        return "a documentation address"
    if address.is_private or address.is_multicast or not address.is_global:
        return "a private or reserved address"
    return None


def checked(values: list[str]) -> list[str]:
    """The addresses, each a distinct public IPv4, at most MAX_HOSTS; refused otherwise."""
    found: list[str] = []
    for value in values:
        try:
            address = ipaddress.IPv4Address(value.strip())
        except ValueError:
            raise deploy.DeployError(f"{value!r} is not an IPv4 address") from None
        reason = _refusal(address)
        if reason:
            raise deploy.DeployError(f"{address} is {reason}; give the host's public IPv4")
        if str(address) in found:
            raise deploy.DeployError(f"{address} is given twice")
        found.append(str(address))
    if len(found) > MAX_HOSTS:
        raise deploy.DeployError(f"{len(found)} addresses: at most {MAX_HOSTS} browser hosts")
    return found


def node_rules(rules: dict[str, Any], port: int | None) -> list[dict[str, Any]]:
    """The inbound rules for the node API port (none while it is closed)."""
    if not port:
        return []
    inbound = rules.get("inbound_rules") or []
    return [r for r in inbound if r.get("ports") == str(port) and r.get("protocol") == "tcp"]


def holds(firewall: dict[str, Any] | None, port: int | None, wanted: list[str]) -> bool:
    """True when the firewall read back has exactly one node API rule, with the pulse-node tag
    and exactly `wanted` as single-address sources (DigitalOcean may answer "A" or "A/32").
    With the port closed there is no such rule to check."""
    if firewall is None:
        return False
    if not port:
        return True
    found = node_rules(firewall, port)
    if len(found) != 1:
        return False
    sources = found[0].get("sources") or {}
    try:
        nets = [ipaddress.IPv4Network(a) for a in sources.get("addresses") or []]
    except ValueError:
        return False
    if any(net.prefixlen != 32 for net in nets):
        return False
    hosts = sorted(str(net.network_address) for net in nets)
    return sources.get("tags") == [PULSE_TAG] and hosts == sorted(wanted)


async def set_browser_hosts(
    cloud: DigitalOcean, inventory_path: Path, addresses: list[str], apply: bool = False
) -> dict[str, Any]:
    """Set exactly `addresses` (an empty list clears them) on the node API rule and record them;
    the dry run changes nothing and prints the node API rule before and after."""
    wanted = checked(addresses)
    inventory = manager_daily.recorded(inventory_path)
    await deploy.check_account(cloud, inventory, inventory.get("account", ""))
    if not inventory.get("manager_firewall_id") or not inventory.get("mac_ipv4"):
        raise deploy.DeployError("the manager has no recorded firewall: deploy first")
    port = inventory.get("node_api_port")
    if wanted and not port:
        raise deploy.DeployError("the node API port is closed: orchestrator-enable first")
    path = f"/v2/firewalls/{inventory['manager_firewall_id']}"
    current = await cloud.get_resource(path, "firewall")
    if current is None:
        raise deploy.DeployError("the recorded manager firewall is gone: deploy first")
    rules = manager_rules(inventory | {BROWSER_HOSTS: wanted}, inventory["mac_ipv4"])
    result = {
        "applied": apply,
        "node_api_rule_before": node_rules(current, port),
        "node_api_rule_after": node_rules(rules, port),
        BROWSER_HOSTS: wanted,
    }
    if not apply:
        return result
    scope = deploy.manager_scope(inventory)
    await ops_api.apply_firewall(cloud, scope, rules, inventory["manager_firewall_id"])
    if not holds(await cloud.get_resource(path, "firewall"), port, wanted):
        raise deploy.DeployError(
            "the manager firewall read back does not hold exactly the requested browser "
            "sources; the inventory is unchanged: compare it with the DigitalOcean console"
        )
    deploy.save(inventory_path, inventory, **{BROWSER_HOSTS: wanted})
    return result


def add_parser(steps) -> None:
    """`campaign manager browser-hosts --ip A[,B...] | --clear [--apply]`."""
    step = steps.add_parser(
        "browser-hosts", help="Admit the browser hosts to the node API port (dry run)"
    )
    which = step.add_mutually_exclusive_group(required=True)
    which.add_argument(
        "--ip", action="append", help="the hosts' public IPv4s, comma-separated (repeatable)"
    )
    which.add_argument("--clear", action="store_true", help="remove every browser host")
    step.add_argument("--apply", action="store_true", help="act; without it, only print")
    step.add_argument(
        "--inventory", type=Path, required=True, help="the proxy-index account's inventory"
    )


def addresses(args) -> list[str]:
    """The --ip values split on commas ([] for --clear). An empty entry is refused, so a failed
    `--ip "$(...)"` never clears the hosts: only --clear does."""
    found = [a.strip() for value in args.ip or [] for a in value.split(",")]
    if "" in found:
        raise deploy.DeployError("--ip has an empty address; use --clear to remove the hosts")
    return found


async def run(args, cloud_for) -> dict[str, Any]:
    """The step on the account the inventory records (proxy-index only, as MANAGER-ORCH-1)."""
    wanted = checked(addresses(args))  # refused before any call
    manager_daily.recorded(args.inventory)
    async with cloud_for(manager_daily.ACCOUNT) as cloud:
        return await set_browser_hosts(cloud, args.inventory, wanted, apply=args.apply)
