"""MANAGER-ORCH-1: the orchestrator on the campaign manager, the one ingest service (K7).

`campaign manager resize` checks the recorded manager's size against DigitalOcean and records it
(`s-8vcpu-16gb-amd` by default). It refuses a size not offered in the manager's recorded region
(`manager_region`, else lon1), and it never resizes a
live manager by itself: a different size is refused with the manual step.

`campaign manager orchestrator-enable` installs `atlas-orchestrator.service` and the
nightly `db-server analyze` timer, opens the node API port (NODE_API_PORT, HTTPS) in the
manager's cloud firewall to the pulse-node tag, and starts both units. The orchestrator reads the
manager's database file in place (`/etc/atlas/database.toml`, never copied or printed).
Its TLS key and admin password are made on the manager and never leave it; only the public
certificate comes back, beside the inventory, with its SHA-256 recorded. The browser hosts'
client CA is made there once too (`--new-browser-ca` replaces it: every host certificate must
then be issued again with `browser-cert`). Before the start it upserts the targets from the
wheel's configs/targets.toml (`atlas db-seed --only targets`, operators' schedules kept), so
a database seeded before a browser target was added gets it. It also installs the daily test's
own inputs and its hourly timer, which stays OFF (`manager_daily`). Without
`--apply` every command prints what it would do and changes nothing; `--apply` of enable also
needs the owner's recorded go (D13). `orchestrator-disable --apply` stops the units and the
hourly timer, then closes the port.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

from . import deploy, host_layout, manager_daily, manager_pulse, ops_api
from .do_api import ApiError, DigitalOcean
from .image import Shell, run_shell
from .manager_rules import BROWSER_PORT, NODE_API_PORT, manager_rules

WANTED_SIZE = "s-8vcpu-16gb-amd"
ORCHESTRATOR_UNIT = "atlas-orchestrator.service"
ANALYZE_SERVICE, ANALYZE_TIMER = "atlas-db-analyze.service", "atlas-db-analyze.timer"
DAILY = (manager_daily.DAILY_SERVICE, manager_daily.DAILY_TIMER)
PULSES = (manager_pulse.PULSE_SERVICE, manager_pulse.PULSE_TIMER)
UNIT_FILES = (ORCHESTRATOR_UNIT, ANALYZE_SERVICE, ANALYZE_TIMER, *DAILY, *PULSES)
STARTED = (ORCHESTRATOR_UNIT, ANALYZE_TIMER)  # never an hourly timer (owner rule)
DATABASE_FILE = deploy.DATABASE_FILE
SEED_TARGETS = f"{manager_daily.ATLAS} db-seed --only targets --database-file {DATABASE_FILE}"
TLS_DIR = "/opt/atlas-campaign/.secrets/tls"
CERT, KEY = f"{TLS_DIR}/orchestrator.crt", f"{TLS_DIR}/orchestrator.key"
BROWSER_CA, BROWSER_CA_KEY = f"{TLS_DIR}/browser-ca.crt", f"{TLS_DIR}/browser-ca.key"
ENV_FILE = "/opt/atlas-campaign/.secrets/orchestrator.env"
DBIP_DIR = "/opt/atlas-campaign/db-ip"
HEALTH_TRIES = 30  # two seconds apart
HOST_FIREWALL = "the hosting provider's panel"  # MANAGER-HOST-1: no cloud firewall to change
ACTIONS = (
    "resize",
    "orchestrator-enable",
    "orchestrator-disable",
    *manager_daily.ACTIONS,
    *manager_pulse.ACTIONS,
)
PEM_RE = re.compile(r"-----BEGIN CERTIFICATE-----\n[A-Za-z0-9+/=\n]+-----END CERTIFICATE-----\n")


def units(inventory: dict[str, Any] | None = None) -> list[tuple[str, bytes, int]]:
    """The unit files installed under /etc/systemd/system (from the wheel's assets); a host
    inventory's user units for its environment (MANAGER-HOST-1)."""
    layout = host_layout.of(inventory or {})
    if layout is not None:
        return layout.unit_files()
    assets = files("atlas.campaign").joinpath("assets")
    return [(name, assets.joinpath(name).read_bytes(), 0o644) for name in UNIT_FILES]


async def resize(
    cloud: DigitalOcean, inventory_path: Path, size: str = WANTED_SIZE, *, apply: bool = False
) -> dict[str, Any]:
    """Check the manager's live size against `size` and, with `apply`, record it."""
    inventory = manager_daily.recorded(inventory_path)
    if host_layout.of(inventory):
        raise deploy.DeployError("the manager is on an existing server: no droplet to resize")
    await deploy.check_account(cloud, inventory, inventory.get("account", ""))
    region = deploy.manager_region(inventory, None)
    if region not in (await cloud.sizes()).get(size, []):
        raise deploy.DeployError(f"size {size} is not offered in {region}")
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


def browser_ca_lines(new: bool = False) -> list[str]:
    """Make the browser hosts' client CA once, root-only (with `new`, a fresh one)."""
    return [
        *([f"rm -f {BROWSER_CA} {BROWSER_CA_KEY}"] if new else []),
        f"if [ ! -s {BROWSER_CA_KEY} ] || [ ! -s {BROWSER_CA} ]; then",
        "  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes "
        "-days 3650 -subj /CN=atlas-browser-ca "
        "-addext basicConstraints=critical,CA:TRUE -addext keyUsage=critical,keyCertSign "
        f"-keyout {BROWSER_CA_KEY}.partial -out {BROWSER_CA}.partial 2>/dev/null",
        f"  mv -f {BROWSER_CA_KEY}.partial {BROWSER_CA_KEY} "
        f"&& mv -f {BROWSER_CA}.partial {BROWSER_CA}",
        "fi",
    ]


def enable_script(host: str, cid: str, port: int = NODE_API_PORT, new_ca: bool = False) -> str:
    """Root shell run on the manager with a tar on stdin (units/, formulas/): check and install
    the daily inputs, then the units; make the TLS key and certificate (for the manager's
    address), the browser CA and the admin password once, root-only; seed the targets; start the
    orchestrator and the analyze timer (never the hourly one); wait for /health over HTTPS; print
    the certificate."""
    address = str(ipaddress.IPv4Address(host))
    url = f"https://{address}:{port}/health"
    curl = f"curl -fsS --max-time 5 --cacert {CERT} --connect-to ::127.0.0.1: {url}"
    return "\n".join(
        [
            "set -eu",
            f"test -s {DATABASE_FILE}",
            f'test "$(stat -c %a {DATABASE_FILE})" = 600',
            "umask 077",
            "stage=$(mktemp -d /root/atlas-orch-XXXXXX)",
            "trap 'rm -rf \"$stage\"' EXIT",
            'tar -x --no-same-owner -C "$stage"',
            *manager_daily.install_lines(cid),
            "umask 022",
            'install -m 644 "$stage"/units/* /etc/systemd/system/',
            f"install -d -m 755 {DBIP_DIR}",
            "umask 077",
            f"install -d -m 700 {TLS_DIR}",
            f"if [ ! -s {KEY} ] || [ ! -s {CERT} ]; then",
            "  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes "
            f"-days 825 -subj /CN=atlas-orchestrator -addext subjectAltName=IP:{address} "
            f"-keyout {KEY}.partial -out {CERT}.partial 2>/dev/null",
            f"  mv -f {KEY}.partial {KEY} && mv -f {CERT}.partial {CERT}",
            "fi",
            *browser_ca_lines(new_ca),
            f"if [ ! -s {ENV_FILE} ]; then",
            "  printf 'ADMIN_USER=admin\\nADMIN_PASSWORD=%s\\n' \"$(openssl rand -hex 24)\" "
            f"> {ENV_FILE}.partial && mv -f {ENV_FILE}.partial {ENV_FILE}",
            "fi",
            f"{SEED_TARGETS} > /dev/null",
            "systemctl daemon-reload",
            f"systemctl enable {' '.join(STARTED)}",
            f"systemctl restart {' '.join(STARTED)}",  # a rerun after a deploy takes the new wheel
            f"for i in $(seq {HEALTH_TRIES}); do {curl} > /dev/null 2>&1 && break; sleep 2; done",
            f"{curl} > /dev/null",
            f"cat {CERT}",
            "echo healthy",
        ]
    )


def payload(formulas: dict[str, bytes], inventory: dict[str, Any] | None = None) -> bytes:
    """The enable script's stdin: units/<unit> and formulas/<network>.json."""
    return deploy._tar(
        [(f"units/{name}", data, mode) for name, data, mode in units(inventory)]
        + [(f"formulas/{name}", data, 0o600) for name, data in formulas.items()]
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
            raise deploy.DeployError(f"DigitalOcean refused the firewall rules: {error}") from None
        raise


def _ports(layout: host_layout.Layout | None) -> dict[str, int]:
    if layout is None:
        return {"node_api_port": NODE_API_PORT, "browser_port": BROWSER_PORT}
    return {"node_api_port": layout.node_port, "browser_port": layout.browser_port}


def _rules(inventory: dict[str, Any], layout: host_layout.Layout | None) -> dict[str, Any]:
    """The manager firewall's rules; a host's firewall is its provider's panel (none here)."""
    if layout is not None:
        return {"inbound_rules": HOST_FIREWALL}
    return manager_rules(inventory, inventory["mac_ipv4"])


def _host_script(inventory: dict[str, Any], script: str) -> str:
    """The enable script for the inventory's manager: a host's user units need their folder."""
    if not host_layout.of(inventory):
        return script
    return 'mkdir -p "$HOME/.config/systemd/user"\n' + host_layout.script(inventory, script)


@dataclass(frozen=True)
class EnableInputs:
    """`orchestrator-enable` inputs: the owner's go (for --apply), the repository's formulas
    and the daily template campaign (needed once, then the inventory's)."""

    owner_go: Path | None = None
    formulas: Path = Path("formulas")
    daily_template: str | None = None
    new_browser_ca: bool = False


async def _check_enable(cloud: DigitalOcean, inventory: dict[str, Any]) -> None:
    """The account, the recorded manager size, and the hourly test off, before enable."""
    await deploy.check_account(cloud, inventory, inventory.get("account", ""))
    if not host_layout.of(inventory) and inventory.get("manager_size") != WANTED_SIZE:
        raise deploy.DeployError(f"record the manager's size first ({WANTED_SIZE}): resize")
    if inventory.get("hourly", {}).get("on"):  # the formulas are never swapped under it
        raise deploy.DeployError(manager_daily.BUSY)


async def enable(
    cloud: DigitalOcean,
    inventory_path: Path,
    inputs: EnableInputs | None = None,
    *,
    apply: bool = False,
    shell: Shell = run_shell,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Install and start the orchestrator on the manager, and install (never start) the hourly
    daily test with its checked inputs; the dry run changes nothing."""
    inputs, inventory = inputs or EnableInputs(), manager_daily.recorded(inventory_path)
    await _check_enable(cloud, inventory)
    cid = manager_daily.template_id(inventory, inputs.daily_template)
    formulas = manager_daily.formula_files(inputs.formulas)
    sent, layout = manager_daily.digests(formulas), host_layout.of(inventory)
    ports = _ports(layout)
    rules = _rules(inventory | ports, layout)
    result = {
        "applied": apply,
        "units": {name: hashlib.sha256(data).hexdigest() for name, data, _ in units(inventory)},
        "seed_targets": host_layout.script(inventory, SEED_TARGETS),
        "started": list(layout.started if layout else STARTED),
        "hourly_timer": "installed off: hourly-enable after the owner's go",
        "daily": {"template": cid, "formulas": len(formulas)},
        **ports,
        "new_browser_ca": inputs.new_browser_ca,
        "firewall_inbound": rules["inbound_rules"],
    }
    if not apply:
        return result
    go = manager_daily.go_record(inputs.owner_go, now or datetime.now(UTC))
    if layout is None:
        await _firewall(cloud, inventory, rules)
    deploy.save(inventory_path, inventory, **ports)
    remote = deploy.Manager(inventory, inventory_path, shell)
    found = enable_script(inventory["host"], cid, ports["node_api_port"], inputs.new_browser_ca)
    script = remote.argv(_host_script(inventory, found))
    code, out = await remote.shell(script, payload(formulas, inventory))
    check = manager_daily.verify(out, code, sent, "orchestrator-enable")
    pem = _certificate(out)
    cert_path = inventory_path.with_name(f"{inventory_path.stem}.orchestrator.crt")
    cert_path.write_text(pem)
    record = go | {
        "cert": cert_path.name,
        "cert_sha256": hashlib.sha256(pem.encode()).hexdigest(),
        "orchestrator_url": f"https://{inventory['host']}:{ports['node_api_port']}",
        "formulas_sha256": sent,
        "droplets_by_hour": check["droplets_by_hour"],
    }
    deploy.save(inventory_path, inventory, orchestrator=record, daily_template=cid)
    return result | {"cert": cert_path.name, "cert_sha256": record["cert_sha256"]}


async def disable(
    cloud: DigitalOcean,
    inventory_path: Path,
    *,
    apply: bool = False,
    shell: Shell = run_shell,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Stop the orchestrator, the analyze timer and the hourly timer, then close the port."""
    inventory = manager_daily.recorded(inventory_path)
    await deploy.check_account(cloud, inventory, inventory.get("account", ""))
    closed = {k: v for k, v in inventory.items() if k not in ("node_api_port", "browser_port")}
    layout = host_layout.of(inventory)
    rules = _rules(closed, layout)
    stopped = [*STARTED, manager_daily.DAILY_TIMER, manager_pulse.PULSE_TIMER]
    result = {
        "applied": apply,
        "stopped": [host_layout.script(inventory, unit) for unit in stopped],
        "firewall_inbound": rules["inbound_rules"],
    }
    if not apply:
        return result
    script = [
        f"systemctl disable --now {' '.join(STARTED)}",
        *manager_daily.off_lines(),
        *manager_pulse.off_lines(),
    ]
    await deploy.Manager(inventory, inventory_path, shell).run(
        host_layout.script(inventory, "set -eu\n" + "\n".join(script)), "orchestrator-disable"
    )
    if layout is None:
        await _firewall(cloud, inventory, rules)
    inventory.pop("node_api_port", None)
    inventory.pop("browser_port", None)
    stamp = (now or datetime.now(UTC)).isoformat()
    hourly = {"on": False, "at": stamp}  # off_lines took both hourly timers out too
    pulses = {"pulses": hourly} if "pulses" in inventory else {}
    deploy.save(inventory_path, inventory, orchestrator_disabled_at=stamp, hourly=hourly, **pulses)
    return result


def add_parsers(steps) -> None:
    """`campaign manager resize|orchestrator-enable|orchestrator-disable|hourly-enable|
    hourly-disable|pulse-enable|pulse-disable` (all dry runs unless --apply)."""
    found = steps.add_parser("resize", help="Check and record the manager's size (dry run)")
    found.add_argument("--size", default=WANTED_SIZE)
    on = steps.add_parser("orchestrator-enable", help="Run the orchestrator here (dry run)")
    on.add_argument("--formulas", type=Path, default=Path("formulas"), help="formulas/*.json")
    on.add_argument("--daily-template", help="the daily template's campaign id (once)")
    on.add_argument(
        "--new-browser-ca",
        action="store_true",
        help="replace the browser hosts' client CA (then reissue every browser-cert)",
    )
    off = steps.add_parser("orchestrator-disable", help="Stop it and close its port (dry run)")
    hourly = steps.add_parser("hourly-enable", help="Start the hourly daily test (owner's go)")
    quiet = steps.add_parser("hourly-disable", help="Stop the hourly daily test (dry run)")
    pulses = steps.add_parser("pulse-enable", help="Start the hourly pulses (owner's go)")
    still = steps.add_parser("pulse-disable", help="Stop the hourly pulses (dry run)")
    for step in (on, hourly, pulses):
        step.add_argument("--owner-go", type=Path, help="the owner's recorded go (for --apply)")
    for step in (found, on, off, hourly, quiet, pulses, still):
        step.add_argument("--apply", action="store_true", help="act; without it, only print")
        step.add_argument(
            "--inventory", type=Path, required=True, help="the atlas account's inventory"
        )


async def run(args, cloud_for) -> dict[str, Any]:
    """One of ACTIONS, on the account the inventory records (as allow-me and teardown)."""
    if args.manager_action == "hourly-enable":
        return await manager_daily.hourly_enable(
            args.inventory, apply=args.apply, owner_go=args.owner_go
        )
    if args.manager_action == "hourly-disable":
        return await manager_daily.hourly_disable(args.inventory, apply=args.apply)
    if args.manager_action == "pulse-enable":
        return await manager_pulse.pulse_enable(
            args.inventory, apply=args.apply, owner_go=args.owner_go
        )
    if args.manager_action == "pulse-disable":
        return await manager_pulse.pulse_disable(args.inventory, apply=args.apply)
    manager_daily.recorded(args.inventory)  # the atlas manager, before any call
    async with cloud_for(manager_daily.ACCOUNT) as cloud:
        if args.manager_action == "resize":
            return await resize(cloud, args.inventory, args.size, apply=args.apply)
        if args.manager_action == "orchestrator-enable":
            inputs = EnableInputs(
                args.owner_go, args.formulas, args.daily_template, args.new_browser_ca
            )
            return await enable(cloud, args.inventory, inputs, apply=args.apply)
        return await disable(cloud, args.inventory, apply=args.apply)
