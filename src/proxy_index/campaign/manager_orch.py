"""MANAGER-ORCH-1: the orchestrator on the campaign manager, the one ingest service (K7).

`campaign manager resize` checks the recorded manager's size against DigitalOcean and records it
(`s-8vcpu-16gb-amd` by default). It refuses a size not offered in lon1, and it never resizes a
live manager by itself: a different size is refused with the manual step.

`campaign manager orchestrator-enable` installs `proxy-index-orchestrator.service` and the
nightly `db-server analyze` timer, opens the node API port (NODE_API_PORT, HTTPS) in the
manager's cloud firewall to the pulse-node tag, and starts both units. The orchestrator reads the
manager's database file in place (`/etc/proxy-index/database.toml`, never copied or printed).
Its TLS key and admin password are made on the manager and never leave it; only the public
certificate comes back, beside the inventory, with its SHA-256 recorded. Without `--apply`
both commands print what they would do and change nothing; `--apply` of enable also needs the
owner's recorded go (D13). `orchestrator-disable --apply` stops the units, then closes the port.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

from . import deploy, ops_api
from .contracts import PULSE_TAG
from .do_api import ApiError, DigitalOcean
from .image import Shell, run_shell

NODE_API_PORT = 8443
WANTED_SIZE = "s-8vcpu-16gb-amd"
ORCHESTRATOR_UNIT = "proxy-index-orchestrator.service"
ANALYZE_TIMER = "proxy-index-db-analyze.timer"
UNIT_FILES = (ORCHESTRATOR_UNIT, "proxy-index-db-analyze.service", ANALYZE_TIMER)
STARTED = (ORCHESTRATOR_UNIT, ANALYZE_TIMER)
DATABASE_FILE = "/etc/proxy-index/database.toml"
TLS_DIR = "/opt/proxy-index-campaign/.secrets/tls"
CERT, KEY = f"{TLS_DIR}/orchestrator.crt", f"{TLS_DIR}/orchestrator.key"
ENV_FILE = "/opt/proxy-index-campaign/.secrets/orchestrator.env"
DBIP_DIR = "/opt/proxy-index-campaign/db-ip"
HEALTH_TRIES = 30  # two seconds apart
ACTIONS = ("resize", "orchestrator-enable", "orchestrator-disable")
PEM_RE = re.compile(r"-----BEGIN CERTIFICATE-----\n[A-Za-z0-9+/=\n]+-----END CERTIFICATE-----\n")


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


def units() -> list[tuple[str, bytes, int]]:
    """The unit files installed under /etc/systemd/system (from the wheel's assets)."""
    assets = files("proxy_index.campaign").joinpath("assets")
    return [(name, assets.joinpath(name).read_bytes(), 0o644) for name in UNIT_FILES]


def _recorded(inventory_path: Path) -> dict[str, Any]:
    inventory = deploy.load_inventory(inventory_path)
    if not inventory.get("manager_id") or not inventory.get("host"):
        raise deploy.DeployError("no manager is recorded")
    return inventory


async def resize(
    cloud: DigitalOcean, inventory_path: Path, size: str = WANTED_SIZE, *, apply: bool = False
) -> dict[str, Any]:
    """Check the manager's live size against `size` and, with `apply`, record it."""
    inventory = _recorded(inventory_path)
    await deploy.check_account(cloud, inventory, inventory.get("account", ""))
    if deploy.MANAGER_REGION not in (await cloud.sizes()).get(size, []):
        raise deploy.DeployError(f"size {size} is not offered in {deploy.MANAGER_REGION}")
    droplet = await cloud.get(str(inventory["manager_id"]))
    if droplet is None:
        raise deploy.DeployError("the recorded manager is gone")
    if droplet.size != size:
        raise deploy.DeployError(
            f"the manager is {droplet.size}, not {size}: with the owner's approval power it "
            "off, resize it (CPU, RAM and disk) and power it on, then run this again"
        )
    recorded = inventory.get("manager_size")
    result = {"manager_size": size, "recorded_size": recorded, "applied": apply}
    if apply and recorded != size:
        deploy.save(inventory_path, inventory, manager_size=size)
    return result | {"action": "none" if recorded == size else "record"}


def go_record(owner_go: Path | None, now: datetime) -> dict[str, str]:
    """The owner's go for the switch (D13): the answer file's path, SHA-256 and the time."""
    if owner_go is None or not owner_go.is_file() or not owner_go.read_text().strip():
        raise deploy.DeployError("--apply needs --owner-go with the owner's recorded answer")
    digest = hashlib.sha256(owner_go.read_bytes()).hexdigest()
    return {"owner_go": str(owner_go.resolve()), "owner_go_sha256": digest, "at": now.isoformat()}


def enable_script(host: str, port: int = NODE_API_PORT) -> str:
    """Root shell run on the manager after the unit files are unpacked from stdin: the TLS key
    and certificate (for the manager's address) and the admin password, each made once and
    root-only; start the units; wait for /health over HTTPS; print the certificate."""
    address = str(ipaddress.IPv4Address(host))
    url = f"https://{address}:{port}/health"
    curl = f"curl -fsS --max-time 5 --cacert {CERT} --connect-to ::127.0.0.1: {url}"
    return "\n".join(
        [
            "set -eu",
            f"test -s {DATABASE_FILE}",
            f'test "$(stat -c %a {DATABASE_FILE})" = 600',
            "umask 022",
            "tar -x --no-same-owner -C /etc/systemd/system",
            f"install -d -m 755 {DBIP_DIR}",
            "umask 077",
            f"install -d -m 700 {TLS_DIR}",
            f"if [ ! -s {KEY} ] || [ ! -s {CERT} ]; then",
            "  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes "
            f"-days 825 -subj /CN=proxy-index-orchestrator -addext subjectAltName=IP:{address} "
            f"-keyout {KEY}.partial -out {CERT}.partial 2>/dev/null",
            f"  mv -f {KEY}.partial {KEY} && mv -f {CERT}.partial {CERT}",
            "fi",
            f"if [ ! -s {ENV_FILE} ]; then",
            "  printf 'ADMIN_USER=admin\\nADMIN_PASSWORD=%s\\n' \"$(openssl rand -hex 24)\" "
            f"> {ENV_FILE}.partial && mv -f {ENV_FILE}.partial {ENV_FILE}",
            "fi",
            "systemctl daemon-reload",
            f"systemctl enable {' '.join(STARTED)}",
            f"systemctl restart {' '.join(STARTED)}",  # a rerun after a deploy takes the new wheel
            f"for i in $(seq {HEALTH_TRIES}); do {curl} > /dev/null 2>&1 && break; sleep 2; done",
            f"{curl} > /dev/null",
            f"cat {CERT}",
            "echo healthy",
        ]
    )


def _certificate(out: str) -> str:
    """The PEM the enable script printed before its last line, `healthy`."""
    start, end = out.find("-----BEGIN CERTIFICATE-----"), out.find("-----END CERTIFICATE-----")
    pem = out[start : end + len("-----END CERTIFICATE-----")] + "\n"
    if out.strip().splitlines()[-1:] != ["healthy"] or start < 0 or not PEM_RE.fullmatch(pem):
        raise deploy.DeployError("the orchestrator did not answer /health over HTTPS")
    return pem


async def _firewall(cloud: DigitalOcean, inventory: dict[str, Any], rules: dict) -> str:
    """Re-apply the manager's recorded firewall with `rules`; a refused rule names the cause."""
    try:
        return await ops_api.apply_firewall(
            cloud, deploy.manager_scope(inventory), rules, inventory["manager_firewall_id"]
        )
    except ApiError as error:
        if error.status == 422:
            raise deploy.DeployError(
                f"DigitalOcean refused the firewall rules (does the tag {PULSE_TAG} exist?)"
            ) from None
        raise


async def enable(
    cloud: DigitalOcean,
    inventory_path: Path,
    *,
    apply: bool = False,
    owner_go: Path | None = None,
    shell: Shell = run_shell,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Install and start the orchestrator on the manager; the dry run changes nothing."""
    inventory = _recorded(inventory_path)
    await deploy.check_account(cloud, inventory, inventory.get("account", ""))
    if inventory.get("manager_size") != WANTED_SIZE:
        raise deploy.DeployError(f"record the manager's size first ({WANTED_SIZE}): resize")
    planned = inventory | {"node_api_port": NODE_API_PORT}
    rules = manager_rules(planned, inventory["mac_ipv4"])
    result = {
        "applied": apply,
        "units": {name: hashlib.sha256(data).hexdigest() for name, data, _ in units()},
        "started": list(STARTED),
        "node_api_port": NODE_API_PORT,
        "firewall_inbound": rules["inbound_rules"],
    }
    if not apply:
        return result
    go = go_record(owner_go, now or datetime.now(UTC))
    await _firewall(cloud, inventory, rules)
    deploy.save(inventory_path, inventory, node_api_port=NODE_API_PORT)
    remote, tar = deploy.Manager(inventory, inventory_path, shell), deploy._tar(units())
    out = await remote.run(enable_script(inventory["host"]), "orchestrator-enable", tar)
    pem = _certificate(out)
    cert_path = inventory_path.with_name(f"{inventory_path.stem}.orchestrator.crt")
    cert_path.write_text(pem)
    record = go | {
        "cert": cert_path.name,
        "cert_sha256": hashlib.sha256(pem.encode()).hexdigest(),
        "orchestrator_url": f"https://{inventory['host']}:{NODE_API_PORT}",
    }
    deploy.save(inventory_path, inventory, orchestrator=record)
    return result | {"cert": cert_path.name, "cert_sha256": record["cert_sha256"]}


async def disable(
    cloud: DigitalOcean,
    inventory_path: Path,
    *,
    apply: bool = False,
    shell: Shell = run_shell,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Stop the orchestrator and the analyze timer, then close the node API port."""
    inventory = _recorded(inventory_path)
    await deploy.check_account(cloud, inventory, inventory.get("account", ""))
    closed = {key: value for key, value in inventory.items() if key != "node_api_port"}
    rules = manager_rules(closed, inventory["mac_ipv4"])
    result = {
        "applied": apply,
        "stopped": list(STARTED),
        "firewall_inbound": rules["inbound_rules"],
    }
    if not apply:
        return result
    await deploy.Manager(inventory, inventory_path, shell).run(
        f"systemctl disable --now {' '.join(STARTED)}", "orchestrator-disable"
    )
    await _firewall(cloud, inventory, rules)
    inventory.pop("node_api_port", None)
    stamp = (now or datetime.now(UTC)).isoformat()
    deploy.save(inventory_path, inventory, orchestrator_disabled_at=stamp)
    return result


def add_parsers(steps, inventory: Path) -> None:
    """`campaign manager resize|orchestrator-enable|orchestrator-disable` (all dry runs unless
    --apply)."""
    found = steps.add_parser("resize", help="Check and record the manager's size (dry run)")
    found.add_argument("--size", default=WANTED_SIZE)
    on = steps.add_parser("orchestrator-enable", help="Run the orchestrator here (dry run)")
    on.add_argument("--owner-go", type=Path, help="the owner's recorded go (needed by --apply)")
    off = steps.add_parser("orchestrator-disable", help="Stop it and close its port (dry run)")
    for step in (found, on, off):
        step.add_argument("--apply", action="store_true", help="act; without it, only print")
        step.add_argument("--inventory", type=Path, default=inventory)


async def run(args, cloud_for) -> dict[str, Any]:
    """One of ACTIONS, on the account the inventory records (as allow-me and teardown)."""
    account = deploy.load_inventory(args.inventory).get("account")
    if account is None:
        raise deploy.DeployError("no manager is recorded")
    async with cloud_for(account) as cloud:
        if args.manager_action == "resize":
            return await resize(cloud, args.inventory, args.size, apply=args.apply)
        if args.manager_action == "orchestrator-enable":
            return await enable(cloud, args.inventory, apply=args.apply, owner_go=args.owner_go)
        return await disable(cloud, args.inventory, apply=args.apply)
