"""Deploy and run the campaign manager: create, harden, secrets, units, enable (teardown:
`teardown.py`).

The manager is one droplet in lon1 (`s-8vcpu-16gb` unless `--size` names another size offered
there) with every ownership marker, in project Proxy Index, created without the droplet agent
(no web console). Only the Mac's campaign key logs in,
and its firewall admits SSH only from the Mac's public IPv4. The DigitalOcean token is read from
the Mac keychain only by `deploy` and travels only on SSH stdin into a root-only 0600 file;
teardown shreds every secret before the guarded manager delete. Every id is written to the
local inventory (0600) before the next step; the host address appears only there and in `argv`.
`teardown --keep-worker-assets` removes only the manager and its firewall and retires it in the
inventory, so the next `deploy` creates a new manager and reuses the worker snapshot and firewall.
"""

from __future__ import annotations

import asyncio
import io
import ipaddress
import re
import secrets
import shlex
import subprocess
import tarfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

from ..onboarding.files import atomic_json, digest, read_json
from . import manager_rules, ops_api
from .contracts import (
    NETWORK_RE,
    PRODUCTION,
    PROJECT_NAME,
    CampaignSpec,
    Droplet,
    names,
    short_id,
    spec_path,
    tags,
)
from .do_api import CreateError, DigitalOcean, TokenSource
from .hostkeys import known_hosts, known_hosts_option
from .image import BASE_IMAGE, SSH_TRIES, Shell, run_shell
from .tick import timer_calendars

BASE = "/opt/proxy-index-campaign"
SECRETS = f"{BASE}/.secrets"
STAGE = "/root/pi-camp-deploy"
MANAGER_REGION, MANAGER_SIZE = "lon1", "s-8vcpu-16gb"
IP_ECHO = "https://ip-info.com/myip"  # the campaign's own echo target (plan D8)
UNIT_FILES = tuple(
    f"proxy-index-campaign-{name}"
    for name in (
        "tick@.service",
        "tick@.timer",
        "collector@.service",
        "watchdog-armed@.service",
        "watchdog-armed@.timer",
        "watchdog-fired@.service",
        "watchdog-fired@.timer",
    )
)
# What `deploy` returns: ids and names only, never the host.
DEPLOYED = (
    "manager_id",
    "manager_name",
    "manager_size",
    "manager_firewall_id",
    "worker_firewall_id",
    "worker_key_id",
    "worker_key_name",
    "wheel_sha256",
    "credentials",
)


class DeployError(RuntimeError):
    """A manager step that failed or was refused; fixed text, never a host, token or secret."""


def unit_names(cid: str) -> list[str]:
    """The units `enable` starts for one campaign: three timers and the collector."""
    short_id(cid)
    return [
        f"proxy-index-campaign-{name}@{cid}.{kind}"
        for name, kind in (
            ("tick", "timer"),
            ("collector", "service"),
            ("watchdog-armed", "timer"),
            ("watchdog-fired", "timer"),
        )
    ]


def timer_dropins(cid: str, spec: dict) -> list[tuple[str, bytes, int]]:
    """Per-campaign drop-ins that move the three timers to the spec's slots; none for the
    hourly default, whose times the unit files already hold."""
    slot = spec.get("slot_minutes", 60)
    if slot == 60:
        return []
    found = timer_calendars(slot, spec.get("create_lead_seconds", 90))
    roles = {
        "tick": "launch",
        "watchdog-armed": "watchdog (armed)",
        "watchdog-fired": "watchdog (fired)",
    }
    dropins = []
    for name, calendar in found.items():
        text = (
            f"# CAMP-1: campaign {cid} has a slot every {slot} minutes.\n[Unit]\n"
            f"Description=Proxy Index campaign %i: {roles[name]} every {slot} min at "
            f"{calendar.removeprefix('*-*-* ')}\n[Timer]\n"
            f"OnCalendar=\nOnCalendar={calendar}\n"
        )
        dropins.append(
            (f"proxy-index-campaign-{name}@{cid}.timer.d/slots.conf", text.encode(), 0o644)
        )
    return dropins


def load_inventory(path: Path) -> dict[str, Any]:
    return read_json(path) if path.exists() else {}


def save(path: Path, inventory: dict[str, Any], **values: Any) -> dict[str, Any]:
    """Update the private inventory atomically (file 0600, folder 0700)."""
    inventory.update(values)
    atomic_json(path, inventory)
    return inventory


async def check_account(cloud: DigitalOcean, inventory: dict[str, Any], account: str) -> str:
    """The client must act for the inventory's manager: the same account key and the same
    Proxy Index project id (each team has its own). Returns the project id."""
    project_id = await cloud.project_id(PROJECT_NAME)
    if (inventory.get("account", account), inventory.get("project_id", project_id)) != (
        account,
        project_id,
    ):
        raise DeployError(
            f"the recorded manager is on account {inventory.get('account')}, "
            f"not on this client's account ({account})"
        )
    return project_id


def audit_path(inventory: Path) -> Path:
    return inventory.with_name("campaign-ops-audit.jsonl")


def manager_scope(inventory: dict[str, Any]):
    since, environment = (
        datetime.fromisoformat(inventory["manager_since"]),
        ops_api.inventory_environment(inventory),
    )
    return ops_api.ops_scope(
        inventory["project_id"], since, str(inventory["manager_id"]), environment
    )


def bound_environment(inventory: dict[str, Any], wanted: str) -> str:
    """`wanted`, which must be the inventory's environment (production for one written before
    environments); a new, empty inventory takes it. Each environment has its own inventory."""
    recorded = ops_api.inventory_environment(inventory) if inventory else wanted
    if recorded != wanted:
        raise DeployError(f"this inventory is the {recorded} manager's, not {wanted}")
    return recorded


def checked_ipv4(value: str) -> str:
    try:
        address = ipaddress.IPv4Address(value.strip())
    except ValueError:
        raise DeployError("could not read this Mac's public IPv4") from None
    if address.is_loopback or address.is_unspecified or address.is_multicast:
        raise DeployError("this Mac's address is not a public IPv4")
    return str(address)


def mac_ipv4(run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> str:
    """This Mac's public IPv4, as the echo target sees it."""
    found = run(
        ["curl", "-4", "-fsS", "--max-time", "10", IP_ECHO],
        capture_output=True,
        text=True,
        check=False,
    )
    return checked_ipv4(found.stdout if found.returncode == 0 else "")


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + re.sub(r"[\x00-\x1f\x7f]", lambda m: f"\\u{ord(m[0]):04x}", escaped) + '"'


def toml_table(name: str, values: dict[str, Any]) -> bytes:
    """One `[name]` table of string values, checked by reading it back with `tomllib`."""
    if (
        not values
        or not re.fullmatch(NETWORK_RE, name)
        or any(
            not re.fullmatch(r"[A-Za-z0-9_-]+", key) or type(value) is not str
            for key, value in values.items()
        )
    ):
        raise DeployError(f"credentials for {name} must be a table of strings")
    text = f"[{name}]\n" + "".join(f"{k} = {_toml_string(v)}\n" for k, v in values.items())
    if tomllib.loads(text) != {name: values}:
        raise DeployError(f"credentials for {name} do not round-trip")
    return text.encode()


def credential_files(path: Path, networks: tuple[str, ...]) -> dict[str, bytes]:
    """Split the private credentials file into one single-table file per network (memory only)."""
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    missing = [net for net in networks if not isinstance(data.get(net), dict)]
    if missing:
        raise DeployError(f"credentials file has no table for {', '.join(missing)}")
    return {net: toml_table(net, data[net]) for net in networks}


def _tar(entries: list[tuple[str, bytes, int]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data, mode in entries:
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


@dataclass
class Manager:
    """SSH to the manager as root with the Mac key and the manager's own known-hosts file,
    found beside the inventory at `path` (`known_hosts`)."""

    inventory: dict[str, Any]
    path: Path
    shell: Shell = run_shell

    def argv(self, command: str) -> list[str]:
        inv = self.inventory
        return [
            "ssh",
            "-i",
            inv["identity"],
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            known_hosts_option(known_hosts(self.path, inv)),
            "-o",
            "ConnectTimeout=10",
            f"root@{inv['host']}",
            command,
        ]

    async def run(self, command: str, what: str, stdin: bytes = b"") -> str:
        code, out = await self.shell(self.argv(command), stdin)
        if code != 0:
            raise DeployError(f"manager step failed: {what} (exit {code})")
        return out

    async def wait_ssh(self, sleep: ops_api.Sleep) -> None:
        for _ in range(SSH_TRIES):
            if (await self.shell(self.argv("true"), b""))[0] == 0:
                return
            await sleep(ops_api.POLL_SECONDS)
        raise DeployError("manager SSH never became ready")

    async def put_secret(self, relative: str, data: bytes) -> None:
        """Write one root-only 0600 file under `.secrets/` from stdin; nothing in `argv`."""
        target = shlex.quote(f"{SECRETS}/{relative}")
        await self.run(
            f"umask 077 && cat > {target}.partial && mv -f {target}.partial {target}",
            f"secret {relative}",
            data,
        )


@dataclass(frozen=True)
class DeployJob:
    """`deploy` inputs: the exact-head wheel and requirements, the Mac key, the secrets."""

    account: str
    wheel: Path
    requirements: Path
    mac_key: Path
    inventory: Path
    credentials: dict[str, bytes]
    mac_ipv4: str
    token: TokenSource
    size: str | None = None  # a new manager's size; None: the recorded one, else MANAGER_SIZE
    environment: str = PRODUCTION  # CAMP-STAGING-1: staging runs beside it on its own names

    def payload(self) -> bytes:
        assets = files("proxy_index.campaign").joinpath("assets")
        public = self.mac_key.with_name(self.mac_key.name + ".pub").read_bytes()
        return _tar(
            [
                (
                    "manager-provision.sh",
                    assets.joinpath("manager-provision.sh").read_bytes(),
                    0o700,
                ),
                ("requirements.txt", self.requirements.read_bytes(), 0o600),
                (self.wheel.name, self.wheel.read_bytes(), 0o600),
                ("mac.pub", public, 0o600),
            ]
            + [(f"units/{name}", assets.joinpath(name).read_bytes(), 0o644) for name in UNIT_FILES]
        )


async def deploy(
    cloud: DigitalOcean,
    job: DeployJob,
    *,
    shell: Shell = run_shell,
    sleep: ops_api.Sleep = asyncio.sleep,
    now: datetime | None = None,
) -> dict:
    """Create (once) and harden the manager, then install the wheel, units and secrets.

    Run again with a new wheel to update a recorded manager; it is never created twice."""
    now, path = now or datetime.now(UTC), job.inventory
    inventory = load_inventory(path)
    project_id = await check_account(cloud, inventory, job.account)  # before any write
    environment = bound_environment(inventory, job.environment)
    size = await manager_size(cloud, inventory, job.size)
    if inventory.get("host") and inventory.get("campaigns"):
        await _refuse_while_running(Manager(inventory, path, shell), inventory["campaigns"])
    public = job.mac_key.with_name(job.mac_key.name + ".pub").read_text()
    mac_key = names(environment).dated("mac", now)
    mac_key_id, _, _ = await ops_api.ensure_ssh_key(cloud, mac_key, public)
    save(
        path,
        inventory,
        environment=environment,
        account=job.account,
        project_id=project_id,
        mac_key_id=mac_key_id,
        manager_size=size,
        user="root",
        identity=str(job.mac_key),
        identity_file=str(job.mac_key),
        known_hosts=str(path.with_name(f"{path.stem}.known_hosts")),  # one per inventory
        campaigns_root=f"{BASE}/campaigns",
    )
    await _ensure_manager(cloud, job, inventory, now, sleep)
    scope = manager_scope(inventory)
    rules = manager_rules.manager_rules(inventory, job.mac_ipv4)
    save(
        path,
        inventory,
        mac_ipv4=job.mac_ipv4,
        manager_firewall_id=await ops_api.apply_firewall(
            cloud, scope, rules, inventory.get("manager_firewall_id")
        ),
    )
    remote = Manager(inventory, path, shell)
    worker_public = await _provision(remote, job, sleep)
    await remote.put_secret("do-token", job.token().encode())
    for network, data in job.credentials.items():
        await remote.put_secret(f"credentials/{network}.toml", data)
    worker_key = await _worker_key_name(cloud, environment, now, worker_public)
    key_id, key_name, _ = await ops_api.ensure_ssh_key(cloud, worker_key, worker_public)
    rules = ops_api.worker_firewall(inventory["host"], environment)
    save(
        path,
        inventory,
        worker_key_id=key_id,
        worker_key_name=key_name,
        worker_firewall_id=await ops_api.apply_firewall(
            cloud, scope, rules, inventory.get("worker_firewall_id")
        ),
        wheel_sha256=digest(job.wheel),
        framework_version=job.wheel.name.split("-")[1],
        credentials=sorted({*inventory.get("credentials", []), *job.credentials}),
        deployed_at=now.isoformat(),
    )
    return {key: inventory[key] for key in DEPLOYED}


async def manager_size(cloud: DigitalOcean, inventory: dict[str, Any], wanted: str | None) -> str:
    """The manager's size: a recorded manager keeps its own (one created before sizes were
    recorded is MANAGER_SIZE); a new one takes `wanted`, else the size of an attempt that
    failed before its manager was recorded, else MANAGER_SIZE, and it must be offered in
    MANAGER_REGION."""
    recorded = inventory.get("manager_size", MANAGER_SIZE) if inventory.get("manager_id") else None
    size = wanted or recorded or inventory.get("manager_size") or MANAGER_SIZE
    if recorded and size != recorded:
        raise DeployError(
            f"the recorded manager is {recorded}; tear it down with "
            f"--keep-worker-assets before deploying a {size} manager"
        )
    if not recorded and MANAGER_REGION not in (await cloud.sizes()).get(size, []):
        raise DeployError(f"size {size} is not offered in {MANAGER_REGION}")
    return size


async def _worker_key_name(
    cloud: DigitalOcean, environment: str, now: datetime, public: str
) -> str:
    """The environment's dated worker key name, with a suffix when a kept key of a retired
    manager already holds it (the plan finds the worker key by its unique name)."""
    name, wanted = names(environment).dated("worker", now), public.split()[:2]
    taken = {
        str(k.get("name"))
        for k in await cloud.ssh_keys()
        if str(k.get("public_key", "")).split()[:2] != wanted
    }
    return f"{name}-{secrets.token_hex(2)}" if name in taken else name


async def _refuse_while_running(remote: Manager, campaigns: list[str]) -> None:
    """A deploy reinstalls the venv and the wheel under the units: never while a campaign's
    tick or collector runs. The watchdog timers stay active until `disable` (they only alert)."""
    units = " ".join(
        f"proxy-index-campaign-{name}@{cid}.{kind}"
        for cid in campaigns
        for name, kind in (("tick", "timer"), ("tick", "service"), ("collector", "service"))
    )
    out = await remote.run(
        f"for u in {units}; do systemctl is-active --quiet $u && echo active $u; done; true",
        "check running units",
    )
    if "active " in out:
        raise DeployError("a campaign's units are running; deploy only between campaigns")


async def _ensure_manager(
    cloud: DigitalOcean, job: DeployJob, inventory: dict, now: datetime, sleep: ops_api.Sleep
) -> Droplet:
    """The recorded manager, or a new one of the recorded size: created, moved into Proxy
    Index, then active."""
    if not inventory.get("manager_id"):
        environment = ops_api.inventory_environment(inventory)
        name = names(environment).dated("manager", now, secrets.token_hex(2))
        tag = tags(environment)["manager"]
        save(job.inventory, inventory, manager_name=name, manager_since=now.isoformat())
        try:
            [droplet] = await cloud.create(
                [name],
                region=MANAGER_REGION,
                size=inventory["manager_size"],
                image=BASE_IMAGE,
                tags=[tag],
                ssh_keys=[inventory["mac_key_id"]],
                with_droplet_agent=False,
                monitoring=False,
                ipv6=False,
            )
        except CreateError as error:
            adopted = await cloud.adopt_uncertain([name], tag) if error.uncertain else []
            if not adopted:
                raise
            droplet = adopted[0]
        save(job.inventory, inventory, manager_id=droplet.id)
        try:
            await ops_api.assign(
                cloud, manager_scope(inventory), droplet, audit_path(job.inventory), "manager"
            )
        except ops_api.OpsError:  # removed through the guard (or listed); a rerun starts fresh
            save(
                job.inventory,
                inventory,
                manager_id=None,
                unassigned_managers=[*inventory.get("unassigned_managers", []), droplet.id],
            )
            raise
    ready = await ops_api.wait_droplet(cloud, str(inventory["manager_id"]), "active", sleep)
    save(job.inventory, inventory, host=ready.public_ipv4)
    return ready


async def _provision(remote: Manager, job: DeployJob, sleep: ops_api.Sleep) -> str:
    """Run the provision script; returns the worker key's public part. Ends `hardened`."""
    await remote.wait_ssh(sleep)
    command = (
        f"rm -rf {STAGE} && mkdir -m 700 {STAGE} && tar -x -C {STAGE} && "
        f"bash {STAGE}/manager-provision.sh"
    )
    code, out = await remote.shell(remote.argv(command), job.payload())
    lines = out.strip().splitlines() or [""]
    keys = [
        line.removeprefix("worker-key ")
        for line in lines
        if re.fullmatch(r"worker-key ssh-ed25519 [A-Za-z0-9+/=]+", line)
    ]
    if code != 0 or lines[-1] != "hardened" or len(keys) != 1:
        raise DeployError(f"manager provision failed ({code}): {lines[-1][:200]}")
    return keys[0]


def spec_payload(root: Path) -> bytes:
    """The spec and the per-network manifests and run configs (no credentials, no IPs)."""
    found = [spec_path(root), *sorted(p for p in (root / "networks").rglob("*") if p.is_file())]
    return _tar([(p.relative_to(root).as_posix(), p.read_bytes(), 0o600) for p in found])


async def upload_spec(inventory_path: Path, root: Path, *, shell: Shell = run_shell) -> dict:
    """Copy a planned campaign root to the manager; never over an existing campaign."""
    inventory = load_inventory(inventory_path)
    spec = CampaignSpec.from_json(spec_path(root).read_text())
    if not inventory.get("manager_id") or (
        spec.manager_droplet_id,
        spec.wheel_sha256,
        spec.account,
        spec.project_id,
        spec.environment,
    ) != (
        str(inventory["manager_id"]),
        inventory.get("wheel_sha256"),
        inventory.get("account"),
        inventory.get("project_id"),
        ops_api.inventory_environment(inventory),
    ):
        raise DeployError("the spec does not name the deployed manager, wheel and account")
    target = f"{BASE}/campaigns/{spec.campaign_id}"
    await Manager(inventory, inventory_path, shell).run(
        f"umask 077 && test ! -e {target} && mkdir {target} && tar -x --no-same-owner -C {target}",
        "upload-spec (exists or failed)",
        spec_payload(root),
    )
    campaigns = sorted({*inventory.get("campaigns", []), spec.campaign_id})
    slots = (
        {"slot_minutes": spec.slot_minutes, "create_lead_seconds": spec.create_lead_seconds}
        if spec.slot_minutes != 60
        else {}
    )
    specs = inventory.get("specs", {}) | {
        spec.campaign_id: {"starts_at": spec.starts_at, "synthetic": spec.synthetic, **slots}
    }
    save(inventory_path, inventory, campaigns=campaigns, campaign_id=spec.campaign_id, specs=specs)
    return {"campaign_id": spec.campaign_id, "campaigns": campaigns}


def owner_go_record(cid: str, owner_go: Path | None, spec: dict, now: datetime) -> dict:
    """The owner's go (D9) as recorded: the answer file's path and SHA-256, and the time. A
    real campaign's answer must hold the exact line `Start: <YYYY-MM-DDTHH:00Z>` (its UTC
    start), so an older answer such as the plan approval cannot start it."""
    text = owner_go.read_text() if owner_go is not None and owner_go.is_file() else ""
    if not text.strip():
        raise DeployError("enable needs --owner-go with the owner's recorded answer (D9)")
    line = f"Start: {datetime.fromisoformat(spec['starts_at']).astimezone(UTC):%Y-%m-%dT%H:00Z}"
    if not spec["synthetic"] and line not in text.splitlines():
        raise DeployError(f"the owner's go must hold the line '{line}' (D9)")
    return {
        "campaign_id": cid,
        "owner_go": str(owner_go.resolve()),
        "synthetic": spec["synthetic"],
        "owner_go_sha256": digest(owner_go),
        "enabled_at": now.isoformat(),
    }


async def enable(
    inventory_path: Path,
    cid: str,
    owner_go: Path | None,
    *,
    shell: Shell = run_shell,
    now: datetime | None = None,
) -> dict:
    """Start the tick timer, collector and watchdogs, only with the owner's recorded go (D9).
    A campaign with other than hourly slots first gets its timer drop-ins (`timer_dropins`)."""
    units, inventory = unit_names(cid), load_inventory(inventory_path)
    spec = inventory.get("specs", {}).get(cid)
    if cid not in inventory.get("campaigns", []) or spec is None:
        raise DeployError("campaign was never uploaded to the recorded manager")
    go = owner_go_record(cid, owner_go, spec, now or datetime.now(UTC))
    save(inventory_path, inventory, enabled=go, campaign_id=cid)
    dropins = timer_dropins(cid, spec)
    command = "systemctl enable --now " + " ".join(units)
    if dropins:
        command = (
            "umask 022 && tar -x --no-same-owner -C /etc/systemd/system && "
            f"systemctl daemon-reload && {command}"
        )
    await Manager(inventory, inventory_path, shell).run(
        command, "enable", _tar(dropins) if dropins else b""
    )
    return go | {"units": units}


async def disable(
    inventory_path: Path, cid: str, *, shell: Shell = run_shell, now: datetime | None = None
) -> dict:
    """Stop one campaign's timers and collector (the end of the dry run, or an owner's stop)."""
    units, inventory = unit_names(cid), load_inventory(inventory_path)
    if cid not in inventory.get("campaigns", []):
        raise DeployError("campaign was never uploaded to the recorded manager")
    await Manager(inventory, inventory_path, shell).run(
        "systemctl disable --now " + " ".join(units), "disable"
    )
    disabled = inventory.get("disabled", {}) | {cid: (now or datetime.now(UTC)).isoformat()}
    save(inventory_path, inventory, disabled=disabled)
    return {"campaign_id": cid, "units": units}


async def allow_me(cloud: DigitalOcean, inventory_path: Path, address: str) -> dict:
    """Point the manager firewall's only SSH rule at this Mac's current public IPv4."""
    inventory, address = load_inventory(inventory_path), checked_ipv4(address)
    await check_account(cloud, inventory, inventory.get("account", ""))
    rules = manager_rules.manager_rules(inventory, address)
    firewall = await ops_api.apply_firewall(
        cloud, manager_scope(inventory), rules, inventory["manager_firewall_id"]
    )
    save(inventory_path, inventory, mac_ipv4=address)
    return {"manager_firewall_id": firewall}
