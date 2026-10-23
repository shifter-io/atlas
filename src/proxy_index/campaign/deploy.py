"""Deploy and run the campaign manager: create, harden, secrets, units, enable, teardown.

The manager is one `s-8vcpu-16gb` droplet in lon1 with every ownership marker, in project Proxy
Index, created without the droplet agent (no web console). Only the Mac's campaign key logs in,
and its firewall admits SSH only from the Mac's public IPv4. The DigitalOcean token is read from
the Mac keychain only by `deploy` and travels only on SSH stdin into a root-only 0600 file;
teardown shreds every secret before the guarded manager delete. Every id is written to the
local inventory (0600) before the next step; the host address appears only there and in `argv`.
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
from . import ops_api
from .contracts import (
    MANAGER_TAG,
    NETWORK_RE,
    PROJECT_NAME,
    ROLES,
    WORKER_TAG,
    CampaignSpec,
    Droplet,
    campaign_tag,
    short_id,
    spec_path,
)
from .do_api import CreateError, DigitalOcean, TokenSource
from .image import BASE_IMAGE, SSH_TRIES, Shell, run_shell
from .scope import Ledger, guarded_delete

BASE = "/opt/proxy-index-campaign"
SECRETS = f"{BASE}/.secrets"
STAGE = "/root/pi-camp-deploy"
MANAGER_REGION, MANAGER_SIZE = "lon1", "s-8vcpu-16gb"
IP_ECHO = "https://ip-info.com/myip"  # the campaign's own echo target (plan D8)
UNIT_FILES = tuple(f"proxy-index-campaign-{name}" for name in (
    "tick@.service", "tick@.timer", "collector@.service", "watchdog-armed@.service",
    "watchdog-armed@.timer", "watchdog-fired@.service", "watchdog-fired@.timer"))
SHRED = (f"cd {BASE} && find .secrets -type f -exec shred -u {{}} + && rm -rf .secrets && "
         "test ! -e .secrets && echo shredded")


class DeployError(RuntimeError):
    """A manager step that failed or was refused; fixed text, never a host, token or secret."""


def unit_names(cid: str) -> list[str]:
    """The units `enable` starts for one campaign: three timers and the collector."""
    short_id(cid)
    return [f"proxy-index-campaign-{name}@{cid}.{kind}" for name, kind in (("tick", "timer"), (
        "collector", "service"), ("watchdog-armed", "timer"), ("watchdog-fired", "timer"))]


def load_inventory(path: Path) -> dict[str, Any]:
    return read_json(path) if path.exists() else {}


def save(path: Path, inventory: dict[str, Any], **values: Any) -> dict[str, Any]:
    """Update the private inventory atomically (file 0600, folder 0700)."""
    inventory.update(values)
    atomic_json(path, inventory)
    return inventory


def audit_path(inventory: Path) -> Path:
    return inventory.with_name("campaign-ops-audit.jsonl")


def manager_scope(inventory: dict[str, Any]):
    since = datetime.fromisoformat(inventory["manager_since"])
    return ops_api.ops_scope(inventory["project_id"], since, str(inventory["manager_id"]))


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
    found = run(["curl", "-4", "-fsS", "--max-time", "10", IP_ECHO], capture_output=True,
                text=True, check=False)
    return checked_ipv4(found.stdout if found.returncode == 0 else "")


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + re.sub(r"[\x00-\x1f\x7f]", lambda m: f"\\u{ord(m[0]):04x}", escaped) + '"'


def toml_table(name: str, values: dict[str, Any]) -> bytes:
    """One `[name]` table of string values, checked by reading it back with `tomllib`."""
    if not values or not re.fullmatch(NETWORK_RE, name) or any(
            not re.fullmatch(r"[A-Za-z0-9_-]+", key) or type(value) is not str
            for key, value in values.items()):
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
    """SSH to the manager as root with the Mac key and the manager's own known-hosts file."""

    inventory: dict[str, Any]
    shell: Shell = run_shell

    def argv(self, command: str) -> list[str]:
        inv = self.inventory
        return ["ssh", "-i", inv["identity"], "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                "-o", "StrictHostKeyChecking=accept-new", "-o",
                f"UserKnownHostsFile={inv['known_hosts']}", "-o", "ConnectTimeout=10",
                f"root@{inv['host']}", command]

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
        await self.run(f"umask 077 && cat > {target}.partial && mv -f {target}.partial "
                       f"{target}", f"secret {relative}", data)


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

    def payload(self) -> bytes:
        assets = files("proxy_index.campaign").joinpath("assets")
        public = self.mac_key.with_name(self.mac_key.name + ".pub").read_bytes()
        return _tar([("manager-provision.sh", assets.joinpath("manager-provision.sh")
                      .read_bytes(), 0o700), ("requirements.txt", self.requirements
                     .read_bytes(), 0o600), (self.wheel.name, self.wheel.read_bytes(), 0o600),
                     ("mac.pub", public, 0o600)] + [
            (f"units/{name}", assets.joinpath(name).read_bytes(), 0o644) for name in UNIT_FILES])


async def deploy(cloud: DigitalOcean, job: DeployJob, *, shell: Shell = run_shell,
                 sleep: ops_api.Sleep = asyncio.sleep, now: datetime | None = None) -> dict:
    """Create (once) and harden the manager, then install the wheel, units and secrets.

    Run again with a new wheel to update a recorded manager; it is never created twice."""
    now, path = now or datetime.now(UTC), job.inventory
    inventory = load_inventory(path)
    public = job.mac_key.with_name(job.mac_key.name + ".pub").read_text()
    mac_key_id, _ = await ops_api.ensure_ssh_key(cloud, f"pi-camp-mac-{now:%Y%m%d}", public)
    save(path, inventory, account=job.account, project_id=await cloud.project_id(
        PROJECT_NAME), mac_key_id=mac_key_id, user="root",
         identity=str(job.mac_key), identity_file=str(job.mac_key),
         known_hosts=str(path.with_name("campaign-manager.known_hosts")),
         campaigns_root=f"{BASE}/campaigns")
    manager = await _ensure_manager(cloud, job, inventory, now, sleep)
    scope = manager_scope(inventory)
    rules = ops_api.manager_firewall(job.mac_ipv4, manager.id)
    save(path, inventory, mac_ipv4=job.mac_ipv4, manager_firewall_id=await ops_api.apply_firewall(
        cloud, scope, rules, inventory.get("manager_firewall_id")))
    remote = Manager(inventory, shell)
    worker_public = await _provision(remote, job, sleep)
    await remote.put_secret("do-token", job.token().encode())
    for network, data in job.credentials.items():
        await remote.put_secret(f"credentials/{network}.toml", data)
    name = f"pi-camp-worker-{now:%Y%m%d}"
    key_id, created = await ops_api.ensure_ssh_key(cloud, name, worker_public)
    rules = ops_api.worker_firewall(inventory["host"])
    save(path, inventory, worker_key_id=key_id,
         worker_key_name=name if created else inventory.get("worker_key_name", name),
         worker_firewall_id=await ops_api.apply_firewall(
             cloud, scope, rules, inventory.get("worker_firewall_id")),
         wheel_sha256=digest(job.wheel), framework_version=job.wheel.name.split("-")[1],
         credentials=sorted(job.credentials), deployed_at=now.isoformat())
    keys = ("manager_id", "manager_name", "manager_firewall_id", "worker_firewall_id",
            "worker_key_id", "worker_key_name", "wheel_sha256", "credentials")
    return {key: inventory[key] for key in keys}


async def _ensure_manager(cloud: DigitalOcean, job: DeployJob, inventory: dict, now: datetime,
                          sleep: ops_api.Sleep) -> Droplet:
    """The recorded manager, or a new one: created, moved into Proxy Index, then active."""
    if not inventory.get("manager_id"):
        name = f"pi-camp-manager-{now:%Y%m%d}-{secrets.token_hex(2)}"
        save(job.inventory, inventory, manager_name=name, manager_since=now.isoformat())
        try:
            [droplet] = await cloud.create(
                [name], region=MANAGER_REGION, size=MANAGER_SIZE, image=BASE_IMAGE,
                tags=[MANAGER_TAG], ssh_keys=[inventory["mac_key_id"]],
                with_droplet_agent=False, monitoring=False, ipv6=False)
        except CreateError as error:
            adopted = await cloud.adopt_uncertain([name], MANAGER_TAG) if error.uncertain else []
            if not adopted:
                raise
            droplet = adopted[0]
        save(job.inventory, inventory, manager_id=droplet.id)
        try:
            await ops_api.assign(cloud, manager_scope(inventory), droplet,
                                 audit_path(job.inventory), "manager")
        except ops_api.OpsError:  # removed through the guard (or listed); a rerun starts fresh
            save(job.inventory, inventory, manager_id=None, unassigned_managers=[
                *inventory.get("unassigned_managers", []), droplet.id])
            raise
    ready = await ops_api.wait_droplet(cloud, str(inventory["manager_id"]), "active", sleep)
    save(job.inventory, inventory, host=ready.public_ipv4)
    return ready


async def _provision(remote: Manager, job: DeployJob, sleep: ops_api.Sleep) -> str:
    """Run the provision script; returns the worker key's public part. Ends `hardened`."""
    await remote.wait_ssh(sleep)
    command = (f"rm -rf {STAGE} && mkdir -m 700 {STAGE} && tar -x -C {STAGE} && "
               f"bash {STAGE}/manager-provision.sh")
    code, out = await remote.shell(remote.argv(command), job.payload())
    lines = out.strip().splitlines() or [""]
    keys = [line.removeprefix("worker-key ") for line in lines
            if re.fullmatch(r"worker-key ssh-ed25519 [A-Za-z0-9+/=]+", line)]
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
    if not inventory.get("manager_id") or (spec.manager_droplet_id, spec.wheel_sha256) != (
            str(inventory["manager_id"]), inventory.get("wheel_sha256")):
        raise DeployError("the spec does not name the deployed manager and wheel")
    target = f"{BASE}/campaigns/{spec.campaign_id}"
    await Manager(inventory, shell).run(
        f"umask 077 && test ! -e {target} && mkdir {target} && "
        f"tar -x --no-same-owner -C {target}", "upload-spec (exists or failed)",
        spec_payload(root))
    campaigns = sorted({*inventory.get("campaigns", []), spec.campaign_id})
    save(inventory_path, inventory, campaigns=campaigns, campaign_id=spec.campaign_id)
    return {"campaign_id": spec.campaign_id, "campaigns": campaigns}


def owner_go_record(cid: str, owner_go: Path | None, now: datetime) -> dict:
    """The owner's go (D9) as recorded: the answer file's path and SHA-256, and the time."""
    if owner_go is None or not owner_go.is_file() or not owner_go.read_text().strip():
        raise DeployError("enable needs --owner-go with the owner's recorded answer (D9)")
    return {"campaign_id": cid, "owner_go": str(owner_go.resolve()),
            "owner_go_sha256": digest(owner_go), "enabled_at": now.isoformat()}


async def enable(inventory_path: Path, cid: str, owner_go: Path | None, *,
                 shell: Shell = run_shell, now: datetime | None = None) -> dict:
    """Start the tick timer, collector and watchdogs, only with the owner's recorded go (D9)."""
    go = owner_go_record(cid, owner_go, now or datetime.now(UTC))
    units, inventory = unit_names(cid), load_inventory(inventory_path)
    if cid not in inventory.get("campaigns", []):
        raise DeployError("campaign was never uploaded to the recorded manager")
    save(inventory_path, inventory, enabled=go, campaign_id=cid)
    await Manager(inventory, shell).run("systemctl enable --now " + " ".join(units), "enable")
    return go | {"units": units}


async def allow_me(cloud: DigitalOcean, inventory_path: Path, address: str) -> dict:
    """Point the manager firewall's only SSH rule at this Mac's current public IPv4."""
    inventory, address = load_inventory(inventory_path), checked_ipv4(address)
    rules = ops_api.manager_firewall(address, str(inventory["manager_id"]))
    firewall = await ops_api.apply_firewall(cloud, manager_scope(inventory), rules,
                                            inventory["manager_firewall_id"])
    save(inventory_path, inventory, mac_ipv4=address)
    return {"manager_firewall_id": firewall}


def _remove_agent() -> bool:
    from .guard import remove
    return remove(Path.home())


def check_reports(inventory: dict, reports: list[Path]) -> None:
    """Every recorded campaign needs a clean collect report (0 mismatches, missing, extra)."""
    clean = set()
    for path in reports:
        report = read_json(path)
        if report.get("clean") is True and not any(
                report.get(key) for key in ("mismatches", "missing", "extra")):
            clean.add(report.get("campaign_id"))
    missing = [cid for cid in inventory.get("campaigns", []) if cid not in clean]
    if not reports or missing:
        raise DeployError(f"teardown needs a clean collect report for {missing or 'a campaign'}")


def _owned(inventory: dict, images: dict) -> list[tuple[str, str]]:
    """(kind, id) in teardown order: manager, firewalls, the two keys, snapshots."""
    found = [("droplet", inventory.get("manager_id"))] + [
        (kind, inventory.get(key)) for kind, key in (
            ("firewall", "manager_firewall_id"), ("firewall", "worker_firewall_id"),
            ("ssh_key", "worker_key_id"), ("ssh_key", "mac_key_id"))]
    found += [("snapshot", b.get("snapshot_id")) for b in images.get("builds", [])]
    return [(kind, str(rid)) for kind, rid in found if rid]


async def teardown(cloud: DigitalOcean, inventory_path: Path, reports: list[Path], *,
                   shell: Shell = run_shell,
                   remove_agent: Callable[[], bool] | None = None) -> dict:
    """Section 16: refuse unless collected and no worker lives; stop the units; shred the
    secrets; remove every recorded resource (snapshots: from the image inventory beside the
    manager's) through the guard; remove the Mac agent; prove."""
    inventory = load_inventory(inventory_path)
    if not inventory.get("manager_id"):
        raise DeployError("no manager is recorded")
    check_reports(inventory, reports)
    if await cloud.list_tag(WORKER_TAG):
        raise DeployError("campaign workers are still alive")
    remote, campaigns = Manager(inventory, shell), inventory.get("campaigns", [])
    units = [unit for cid in campaigns for unit in unit_names(cid)]
    if units:
        await remote.run("systemctl disable --now " + " ".join(units), "stop units")
    if (await remote.run(SHRED, "shred secrets")).strip().splitlines()[-1:] != ["shredded"]:
        raise DeployError("manager secrets were not shredded")
    save(inventory_path, inventory, secrets_shredded=True)
    owned = _owned(inventory, load_inventory(inventory_path.with_name("campaign-images.json")))
    ledger = Ledger({kind: {rid for k, rid in owned if k == kind} for kind, _ in owned})
    released = []
    for kind, rid in owned:  # the droplet is the recorded manager; other kinds ignore the role
        try:
            result = await guarded_delete(cloud, kind, rid, manager_scope(inventory), ledger,
                                          audit_path(inventory_path), "manager")
        except Exception as error:  # audited by the guard; the other resources still go
            result = f"failed: {type(error).__name__}"
        released.append({"kind": kind, "id": rid, "result": result})
    tags = [tag for tag, _ in ROLES.values()] + [campaign_tag(c) for c in campaigns]
    proof = {tag: len(await cloud.list_tag(tag)) for tag in tags}
    clean = all(r["result"] in ("deleted", "absent") for r in released) and not any(
        proof.values())
    done = {"released": released, "agent_removed": (remove_agent or _remove_agent)(),
            "tag_counts": proof, "clean": clean}
    save(inventory_path, inventory, teardown=done)
    return done
