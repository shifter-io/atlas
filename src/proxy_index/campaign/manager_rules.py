"""MANAGER-ORCH-1: the manager firewall's rules, shared by `deploy` and `manager_orch`.

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


def node_api_rule(port: int = NODE_API_PORT) -> dict[str, Any]:
    """The node API port, open to the pulse-node tag (BP-FIREWALL adds the minis' addresses)."""
    return {"protocol": "tcp", "ports": str(port), "sources": {"tags": [PULSE_TAG]}}


def manager_rules(inventory: dict[str, Any], mac_ipv4: str) -> dict[str, Any]:
    """The manager firewall: SSH from the Mac, all outbound, and the node API rule while the
    inventory records the orchestrator's port (deploy and allow-me keep it)."""
    rules = ops_api.manager_firewall(mac_ipv4, str(inventory["manager_id"]))
    if inventory.get("node_api_port"):
        rules["inbound_rules"].append(node_api_rule(int(inventory["node_api_port"])))
    return rules
