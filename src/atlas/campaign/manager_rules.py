"""MANAGER-ORCH-1: the manager firewall's rules, shared by `deploy` and `manager_orch`.

SSH from the Mac and all outbound (`ops_api.manager_firewall`), plus the node API rule while the
inventory records the orchestrator's port, and the browser port rule while it records that one
(BROWSER-MTLS-1), so `deploy` and `allow-me` keep them. It imports only `ops_api` and
`contracts`, so `deploy` and `manager_orch` both use it without importing each other.
"""

from __future__ import annotations

from typing import Any

from . import ops_api
from .contracts import PULSE_TAG

NODE_API_PORT = 8443
# BROWSER-MTLS-1: the browser hosts' port. Their home addresses change, so it is open to every
# address; the orchestrator admits only a client certificate of the manager's browser CA.
BROWSER_PORT = 8444


def node_api_rule(port: int = NODE_API_PORT) -> dict[str, Any]:
    """The node API port, open to the pulse-node tag."""
    return {"protocol": "tcp", "ports": str(port), "sources": {"tags": [PULSE_TAG]}}


def browser_rule(port: int = BROWSER_PORT) -> dict[str, Any]:
    """The browser port, open to every IPv4 and IPv6 address."""
    everywhere = list(ops_api.EVERYWHERE["addresses"])
    return {"protocol": "tcp", "ports": str(port), "sources": {"addresses": everywhere}}


def manager_rules(inventory: dict[str, Any], mac_ipv4: str) -> dict[str, Any]:
    """The manager firewall: SSH from the Mac, all outbound, and the node API and browser port
    rules while the inventory records those ports (deploy and allow-me keep them)."""
    environment = ops_api.inventory_environment(inventory)
    rules = ops_api.manager_firewall(mac_ipv4, str(inventory["manager_id"]), environment)
    if inventory.get("node_api_port"):
        rules["inbound_rules"].append(node_api_rule(int(inventory["node_api_port"])))
    if inventory.get("browser_port"):
        rules["inbound_rules"].append(browser_rule(int(inventory["browser_port"])))
    return rules
