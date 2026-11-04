"""MANAGER-ORCH-1: the manager firewall's rules, shared by `deploy`, `manager_orch` and
`browser_hosts`.

SSH from the Mac and all outbound (`ops_api.manager_firewall`), plus the node API rule while the
inventory records the orchestrator's port, so `deploy` and `allow-me` keep it. It imports only
`ops_api` and `contracts`, so `deploy` and `manager_orch` both use it without importing each
other.
"""

from __future__ import annotations

from typing import Any

from . import ops_api
from .contracts import PULSE_TAG

NODE_API_PORT = 8443
BROWSER_HOSTS = "browser_host_ipv4s"  # BP-FIREWALL: the minis' public IPv4s, in the inventory


def node_api_rule(
    port: int = NODE_API_PORT, browser_hosts: list[str] | None = None
) -> dict[str, Any]:
    """The node API port, open to the pulse-node tag and to each browser host's address."""
    sources: dict[str, list[str]] = {"tags": [PULSE_TAG]}
    if browser_hosts:
        sources["addresses"] = [f"{address}/32" for address in browser_hosts]
    return {"protocol": "tcp", "ports": str(port), "sources": sources}


def manager_rules(inventory: dict[str, Any], mac_ipv4: str) -> dict[str, Any]:
    """The manager firewall: SSH from the Mac, all outbound, and the node API rule while the
    inventory records the orchestrator's port (deploy and allow-me keep it, and its browser
    hosts)."""
    rules = ops_api.manager_firewall(mac_ipv4, str(inventory["manager_id"]))
    if inventory.get("node_api_port"):
        port, hosts = int(inventory["node_api_port"]), inventory.get(BROWSER_HOSTS)
        rules["inbound_rules"].append(node_api_rule(port, hosts))
    return rules
