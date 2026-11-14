"""The pulse fleet: one always-on node per network and region (PULSE-1b).

`campaign pulse-fleet reconcile --root R` brings up the fleet that `R/fleet.json` describes: each
wanted node `atlas-pulse-<region>-<network>` that the pulse tag does not list is created
(`s-1vcpu-1gb-amd` by default) with the pulse tag only, moved into Dev Resources through
`scope.assign_and_verify_project` and armed. Arming issues the node's token (rotated), pushes
the wheel, the network's provider config, its credentials and the token on stdin, and installs
the `atlas-pulse-node` service, which runs `atlas node` registered for that network and
region; the orchestrator hands it one country at a time. The node reads the DB-IP MMDB as
`db-ip/dbip-full.mmdb` (arming renames the image's file to that name); with `dbip_base_url`,
arming also installs the `atlas-dbip-update` timer, which runs `atlas dbip update` daily
against that CDN directory and renames each new release over the same name, and the node
reopens the replaced file between runs (DBIP-DAILY-1). Without it, arming disables and removes
a timer an earlier arming installed.

Reconcile only creates: a droplet it did not record, or one no longer wanted, is reported as
`unknown` and never touched, and it never deletes anything (a node whose assignment was not
confirmed ends `destroy_refused`, for the owner). `retire --name N` removes one recorded node,
only through `scope.retire_pulse_node`, and checks it gone; a later reconcile creates it again
unless `fleet.json` no longer lists it. Exit codes: reconcile 1 when a node failed or waits for
the owner (`destroy_refused`), retire 1 when the node was not verified gone; otherwise 0.

The node service runs as root (decision PULSE-1b): the droplet is single-purpose, its secrets
are 0600 under `UMask=0077`, and a dedicated user is a follow-up once the smoke test has
checked its file permissions on a real node.

`fleet.json` keys: account, size (only `s-1vcpu-1gb-amd`), regions, image_by_region,
ssh_key_id, identity (the SSH key file), wheel, orchestrator_url, database_file, optional
dbip_base_url (https), optional ca_file (the manager's certificate, which arming copies to
the node as `pulse/orchestrator-ca.crt`; the node then trusts only it; ca_file needs an https
orchestrator_url, and a remote orchestrator_url must be https either way: node_tls), and
networks: {name: {product, provider_config, credentials, provider (default the name)}}.
Paths are relative to the root. The on-demand hour (pulse_ondemand.py) runs the same pass with
`cells` set: only the hour's (network, region) nodes, with the hour's own ledger. The ledger
`nodes.json` holds names, ids and states only; addresses stay in memory and tokens only in the
stdin tar.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import tarfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import legacy_names
from ..errors import UserError
from ..node_tls import client_tls
from ..onboarding.files import atomic_json, lock
from ..onboarding.scheduler import Clock
from . import scope
from .contracts import (
    ACCOUNTS,
    NETWORK_RE,
    PULSE_PROJECT_NAME,
    PULSE_TAG,
    WORKER_ROOT,
    Droplet,
    DropletRecord,
    with_legacy,
)
from .do_api import CreateError
from .fanout import INSTALL_WHEEL

DEFAULT_SIZE = "s-1vcpu-1gb-amd"
SIZES = (DEFAULT_SIZE,)  # the approved pulse node size; another needs an owner decision
ARM_AT_ONCE = 16  # concurrent SSH sessions and token issues while arming
NODE_SECRETS = f"{WORKER_ROOT}/pulse-secrets"  # kept across reboots: the service restarts
READY_LIMIT_SECONDS = 180
READY_POLL_SECONDS = 2.0
GONE_CHECKS = 6  # reads of a retired node before it counts as left over
GONE_POLL_SECONDS = 5.0
ARMABLE = ("assigned", "booting", "starting", "boot_failed", "start_failed")
REGION_RE = r"^[a-z0-9]+$"
WORD_RE = r"^[A-Za-z0-9_.-]+$"  # provider and product: safe in a systemd ExecStart line
URL_RE = r"^https?://[A-Za-z0-9.:/_-]+$"
DBIP_URL_RE = r"^https://[A-Za-z0-9.:/_-]+$"  # also safe in a systemd ExecStart line
CA_NAME = "orchestrator-ca.crt"  # under pulse/ on the node

# Runs with the node's system Python: argv = root, secrets dir. Writes the secrets owner-only,
# installs the wheel and (re)starts the always-on node service.
NODE_SCRIPT = (
    r"""
import hashlib, io, json, os, pathlib, subprocess, sys, tarfile
root, secrets = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
archive = tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read()), mode="r:")
secrets.mkdir(mode=0o700, parents=True, exist_ok=True)
os.chmod(secrets, 0o700)
for name in ("credentials.toml", "node-token"):
    data = archive.extractfile("secrets/" + name).read()
    fd = os.open(secrets / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, data); os.close(fd)
members = [m for m in archive.getmembers() if m.name.startswith(("pulse/", "wheel/"))]
archive.extractall(root, members=members, filter="data")
wheel = next(root / m.name for m in members if m.name.endswith(".whl"))
"""
    + INSTALL_WHEEL
    + r"""node = json.loads((root / "pulse" / "node.json").read_text())
db, cli = root / "db-ip", str(root / "venv" / "bin" / "atlas")
mmdb = db / "dbip-full.mmdb"  # the one name `dbip update` replaces atomically
databases = sorted(db.glob("*.mmdb"))
if not mmdb.exists() and len(databases) == 1:  # the image's release, until the first update
    databases[0].replace(mmdb)
country_check = ["--mmdb", str(mmdb)] if mmdb.exists() else []
ca = root / "pulse" / "orchestrator-ca.crt"  # CA_NAME
pinned = ["--ca-file", str(ca)] if node.get("ca_file") else []
command = [cli, "node",
           "--orchestrator", node["orchestrator"], "--token-file", str(secrets / "node-token"),
           "--provider", node["provider"], "--product", node["product"],
           "--region", node["region"], "--provider-config", str(root / "pulse" / "provider.toml"),
           "--credentials-file", str(secrets / "credentials.toml"),
           "--work-dir", str(root / "pulse" / "runs"), *country_check, *pinned]
unit = ["[Unit]", "Description=atlas pulse node", "After=network-online.target",
        "Wants=network-online.target", "", "[Service]", f"WorkingDirectory={root}",
        "ExecStart=" + " ".join(command), "Restart=always", "RestartSec=10", "UMask=0077",
        "LimitNOFILE=65536", "", "[Install]", "WantedBy=multi-user.target", ""]
units = {"atlas-pulse-node.service": unit}
steps = [["daemon-reload"], ["enable", "atlas-pulse-node"], ["restart", "atlas-pulse-node"]]
updater = ("atlas-dbip-update.service", "atlas-dbip-update.timer")
if not node.get("dbip_base_url"):  # no longer wanted: stop an earlier arming's timer
    subprocess.run(["systemctl", "disable", "--now", updater[1]], check=False)
    for name in updater:
        pathlib.Path("/etc/systemd/system/" + name).unlink(missing_ok=True)
else:
    update = [cli, "dbip", "update", "--dest", str(db), "--base-url", node["dbip_base_url"]]
    units["atlas-dbip-update.service"] = ["[Unit]", "Description=atlas DB-IP update",
        "After=network-online.target", "Wants=network-online.target", "", "[Service]",
        "Type=oneshot", "ExecStart=" + " ".join(update), "Nice=10", ""]
    units["atlas-dbip-update.timer"] = ["[Unit]", "Description=Daily DB-IP update", "",
        "[Timer]", "OnActiveSec=5min", "OnCalendar=*-*-* 05:41:00 UTC",
        "RandomizedDelaySec=1800", "Persistent=true", "", "[Install]",
        "WantedBy=timers.target", ""]
    steps.append(["enable", "--now", "atlas-dbip-update.timer"])
for name, lines in units.items():
    pathlib.Path("/etc/systemd/system/" + name).write_text("\n".join(lines))
for step in steps:
    subprocess.run(["systemctl", *step], check=True)
"""
)
NODE_COMMAND = ["python3", "-c", NODE_SCRIPT, WORKER_ROOT, NODE_SECRETS]
Issuer = Callable[..., dict]


class FleetError(ValueError):
    """A fleet input or request that cannot run; the message names it, never a secret."""


def node_name(network: str, region: str) -> str:
    return f"atlas-pulse-{region}-{network}"


@dataclass(frozen=True)
class Network:
    product: str
    provider_config: Path
    credentials: Path
    provider: str


@dataclass(frozen=True)
class FleetConfig:
    account: str
    regions: tuple[str, ...]
    image_by_region: dict[str, str]
    ssh_key_id: str
    identity: Path
    wheel: Path
    orchestrator_url: str
    database_file: Path
    networks: dict[str, Network]
    size: str = DEFAULT_SIZE
    dbip_base_url: str | None = None
    ca_file: Path | None = None
    cells: tuple[tuple[str, str], ...] | None = None  # (network, region): an on-demand hour's

    def wanted(self) -> dict[str, tuple[str, str]]:
        """Every node the fleet should have: name -> (network, region); with `cells`, only
        those, else every network in every region."""
        if self.cells is not None:
            return {node_name(n, r): (n, r) for n, r in self.cells}
        return {
            node_name(n, r): (n, r) for n in sorted(self.networks) for r in sorted(self.regions)
        }


def _need(failed: bool, what: str) -> None:
    if failed:
        raise FleetError(f"fleet.json: invalid {what}")


def load_config(root: Path) -> FleetConfig:
    raw = json.loads((root / "fleet.json").read_text())
    regions = tuple(raw.get("regions") or ())
    networks = {}
    for name, entry in (raw.get("networks") or {}).items():
        _need(not re.fullmatch(NETWORK_RE, name), "network name")
        provider = entry.get("provider", name)
        _need(not all(re.fullmatch(WORD_RE, str(v)) for v in (provider, entry["product"])), name)
        paths = (root / entry["provider_config"], root / entry["credentials"])
        networks[name] = Network(entry["product"], *paths, provider)
    config = FleetConfig(
        account=legacy_names.account(str(raw["account"])),
        regions=regions,
        image_by_region=dict(raw["image_by_region"]),
        ssh_key_id=str(raw["ssh_key_id"]),
        identity=root / raw["identity"],
        wheel=root / raw["wheel"],
        orchestrator_url=raw["orchestrator_url"],
        database_file=root / raw["database_file"],
        networks=networks,
        size=raw.get("size", DEFAULT_SIZE),
        dbip_base_url=raw.get("dbip_base_url"),
        ca_file=root / raw["ca_file"] if raw.get("ca_file") else None,
    )
    _need(config.account not in ACCOUNTS, "account")
    _need(config.size not in SIZES, "size")
    _need(not regions or not networks, "regions or networks (none)")
    _need(any(not re.fullmatch(REGION_RE, r) for r in regions), "region")
    _need(any(r not in config.image_by_region for r in regions), "image_by_region")
    _need(not re.fullmatch(URL_RE, config.orchestrator_url), "orchestrator_url")
    _need(
        config.dbip_base_url is not None and not re.fullmatch(DBIP_URL_RE, config.dbip_base_url),
        "dbip_base_url",
    )
    _need(config.wheel.suffix != ".whl", "wheel")
    try:
        client_tls(config.orchestrator_url, config.ca_file)
    except UserError as error:
        raise FleetError(f"fleet.json: invalid orchestrator_url or ca_file ({error})") from None
    return config


def nodes_path(root: Path) -> Path:
    return root / "nodes.json"


def load_nodes(root: Path) -> dict[str, DropletRecord]:
    try:
        raw = json.loads(nodes_path(root).read_text())
    except FileNotFoundError:
        return {}
    return {name: DropletRecord(**item) for name, item in raw["nodes"].items()}


def save_nodes(root: Path, records: dict[str, DropletRecord]) -> None:
    atomic_json(nodes_path(root), {"nodes": {n: asdict(r) for n, r in sorted(records.items())}})


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class FleetClients:
    """The cloud, SSH remote, node-token issuer and clock of one reconcile pass."""

    cloud: Any
    remote: Any
    issuer: Issuer
    clock: Clock


class Fleet:
    """One reconcile pass under the root's lock."""

    def __init__(self, root: Path, config: FleetConfig, clients: FleetClients):
        self.root, self.config = root, config
        self.cloud, self.remote = clients.cloud, clients.remote
        self.issuer, self.clock = clients.issuer, clients.clock
        self.records = load_nodes(root)
        self.report: dict[str, list] = {
            k: [] for k in ("created", "present", "unknown", "failed", "destroy_refused")
        }

    def save(self) -> None:
        save_nodes(self.root, self.records)

    async def reconcile(self) -> dict:
        self.scope = scope.pulse_scope(await self.cloud.project_id(PULSE_PROJECT_NAME))
        await self._adopt()
        # A pre-rename node (legacy_names) is listed too: reported as unknown, never orphaned.
        listed = [d for tag in with_legacy([PULSE_TAG]) for d in await self.cloud.list_tag(tag)]
        wanted = self.config.wanted()
        ours = {r.droplet_id: n for n, r in self.records.items() if r.droplet_id}
        for droplet in listed:
            if ours.get(droplet.id) not in wanted:  # not recorded, or no longer wanted
                self.report["unknown"].append({"id": droplet.id, "name": droplet.name})
        shown = {d.id for d in listed}
        taken = {d.name for d in listed}
        missing = [n for n in wanted if n not in taken and await self._lost(n, shown)]
        await self._create(missing, wanted)
        await self._assign()
        gate = asyncio.Semaphore(ARM_AT_ONCE)
        armable = [r for n, r in self.records.items() if n in wanted and self._armable(r)]
        await asyncio.gather(*(self._arm(r, gate) for r in armable))
        self._sort(set(wanted))
        self.save()
        return self.report

    async def _adopt(self) -> None:
        """Records left `requested` by a process that stopped mid-create: take their droplets
        by exact name under the pulse tag (this also marks them as this client's creates)."""
        waiting = {n: r for n, r in self.records.items() if r.status == "requested"}
        if not waiting:
            return
        found = await self.cloud.adopt_uncertain(sorted(waiting), PULSE_TAG)
        for name, record in waiting.items():
            matches = [d for d in found if d.name == name]
            if record.droplet_id is None and len(matches) == 1:
                record.droplet_id = matches[0].id
        self.save()

    async def _lost(self, name: str, shown: set[str]) -> bool:
        """Whether `name` needs a new droplet: no record, or its recorded one is gone."""
        record = self.records.get(name)
        if record is None or record.droplet_id is None or record.status == "destroyed":
            return True
        if record.droplet_id in shown or await self.cloud.get(record.droplet_id) is not None:
            self.report["failed"].append({"name": name, "error": "not listed under the tag"})
            return False  # it exists without the pulse tag: the owner decides
        record.status, record.destroyed_at = "destroyed", record.destroyed_at or _now()
        return True

    async def _create(self, names: list[str], wanted: dict[str, tuple[str, str]]) -> None:
        for region in sorted({wanted[n][1] for n in names}):
            batch = [n for n in names if wanted[n][1] == region]
            for name in batch:
                old = self.records.get(name)
                network = wanted[name][0]
                self.records[name] = DropletRecord(
                    network, region, 0, name, size=self.config.size,
                    image_id=self.config.image_by_region[region], requested_at=_now(),
                    attempts=(old.attempts if old else 0) + 1,
                )  # fmt: skip
            self.save()  # before the create: a crash leaves `requested`, adopted next time
            for droplet in await self._create_call(batch, region):
                if droplet.name in batch:
                    self.records[droplet.name].droplet_id = droplet.id
                    self.report["created"].append(droplet.name)
            for name in batch:
                if self.records[name].droplet_id is None:
                    self.records[name].status = "create_failed"
            self.save()

    async def _create_call(self, names: list[str], region: str) -> list[Droplet]:
        try:
            return await self.cloud.create(
                names,
                region=region,
                size=self.config.size,
                image=self.config.image_by_region[region],
                tags=[PULSE_TAG],
                ssh_keys=[self.config.ssh_key_id],
            )  # monitoring, the droplet agent and IPv6 stay off
        except CreateError as error:  # an uncertain rest is taken by its exact names
            if not error.uncertain:
                return error.created
            return error.created + await self.cloud.adopt_uncertain(error.pending, PULSE_TAG)

    async def _assign(self) -> None:
        waiting = [r for r in self.records.values() if r.droplet_id and r.status == "requested"]
        if not waiting:
            return
        by_id = {r.droplet_id: r for r in self.records.values() if r.droplet_id}
        await scope.assign_and_verify_project(
            self.cloud,
            self.scope,
            [r.droplet_id for r in waiting],
            by_id,
            self.root / "audit.jsonl",
            "pulse",
            scope.Retry(sleep=self.clock.sleep, save=self.save),
        )
        self.save()

    def _armable(self, record: DropletRecord) -> bool:
        return bool(record.droplet_id) and record.status in ARMABLE

    async def _arm(self, record: DropletRecord, gate: asyncio.Semaphore) -> None:
        async with gate:
            await self._arm_one(record)

    async def _arm_one(self, record: DropletRecord) -> None:
        """Wait for SSH, then push and start the node; any error fails only this node."""
        try:
            record.status = "booting"
            droplet = await self._active(record)
            if droplet is None or not await self.remote.wait_ready(droplet, READY_LIMIT_SECONDS):
                record.status, record.error_class = "boot_failed", "boot_failed"
                return
            record.status, record.ssh_ready_at = "starting", _now()
            payload = await self._bundle(record)
            await self.remote.push_start(droplet, payload, NODE_COMMAND)
            record.status, record.armed_at, record.error_class = "armed", _now(), None
        except Exception as error:  # boundary: per-node isolation; type and scrubbed detail kept
            record.status, record.error_class = "start_failed", type(error).__name__
            record.error_detail = scope.error_detail(self.cloud, [record.droplet_id], error)
        finally:
            self.save()

    async def _active(self, record: DropletRecord) -> Droplet | None:
        deadline = self.clock.now() + READY_LIMIT_SECONDS
        while True:
            found = await self.cloud.get(record.droplet_id)
            if found is not None and found.status == "active" and found.public_ipv4:
                record.active_at = record.active_at or _now()
                return found
            if self.clock.now() + READY_POLL_SECONDS > deadline:
                return None
            await self.clock.sleep(READY_POLL_SECONDS)

    async def _bundle(self, record: DropletRecord) -> bytes:
        """One tar: the node's settings, provider config, wheel, credentials and a new token."""
        network = self.config.networks[record.network]
        issued = await asyncio.to_thread(
            self.issuer,
            str(self.config.database_file),
            provider=network.provider,
            product=network.product,
            region=record.region,
        )
        node = {
            "orchestrator": self.config.orchestrator_url,
            "provider": network.provider,
            "product": network.product,
            "region": record.region,
        }
        if self.config.dbip_base_url:
            node["dbip_base_url"] = self.config.dbip_base_url
        pinned = []
        if self.config.ca_file:
            node["ca_file"] = True
            pinned = [(f"pulse/{CA_NAME}", self.config.ca_file.read_bytes())]
        files = [
            ("pulse/node.json", json.dumps(node).encode()),
            ("pulse/provider.toml", network.provider_config.read_bytes()),
            (f"wheel/{self.config.wheel.name}", self.config.wheel.read_bytes()),
            ("secrets/credentials.toml", network.credentials.read_bytes()),
            ("secrets/node-token", (issued["token"] + "\n").encode()),
            *pinned,
        ]
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for name, data in files:
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = len(data), 0o600, int(self.clock.now())
                archive.addfile(info, io.BytesIO(data))
        return buffer.getvalue()

    def _sort(self, wanted: set[str]) -> None:
        lost = {item["name"] for item in self.report["failed"]}  # exists, but not under the tag
        for name in sorted(wanted & set(self.records) - lost):
            kind, entry = self._outcome(name, self.records[name])
            if kind:
                self.report[kind].append(entry)

    def _outcome(self, name: str, record: DropletRecord) -> tuple[str | None, object]:
        """The report list a wanted node belongs in, and its entry; None for a fresh create."""
        if record.status == "armed" and name not in self.report["created"]:
            return "present", name
        if record.assign_failed_at or record.status in ("destroy_refused", "destroy_failed"):
            return "destroy_refused", {"name": name, "id": record.droplet_id}
        if record.status != "armed":
            return "failed", {"name": name, "status": record.status}
        return None, None


async def reconcile(root: Path, cloud, remote, issuer: Issuer, clock=None) -> dict:
    """Create and arm the missing nodes; never deletes. Returns the report (no addresses)."""
    config = load_config(root)
    with lock(root / ".lock"):
        clients = FleetClients(cloud, remote, issuer, clock or Clock())
        return await Fleet(root, config, clients).reconcile()


async def retire(root: Path, name: str, cloud, clock=None) -> dict:
    """Remove one recorded node through `scope.retire_pulse_node` and check it gone. A refusal
    (`OwnershipError`) leaves it `destroy_refused` and is raised."""
    clock = clock or Clock()
    with lock(root / ".lock"):
        records = load_nodes(root)
        record = records.get(name)
        if record is None or record.droplet_id is None:
            raise FleetError(f"{name} is not a recorded pulse node")
        rid = record.droplet_id
        ledger = scope.Ledger({"droplet": [rid]}, {rid: record})
        project_id = await cloud.project_id(PULSE_PROJECT_NAME)
        try:
            result = await scope.retire_pulse_node(
                cloud, rid, project_id, ledger, root / "audit.jsonl"
            )
        except scope.OwnershipError:
            record.status = "destroy_refused"
            raise
        except Exception:
            record.status = "destroy_failed"
            raise
        finally:
            save_nodes(root, records)
        gone = await _gone(cloud, rid, clock)
        record.status = "destroyed" if gone else "destroy_failed"
        record.destroyed_at = _now() if gone else None
        save_nodes(root, records)
    return {"name": name, "id": rid, "result": result, "gone": gone}


async def _gone(cloud, droplet_id: str, clock) -> bool:
    for attempt in range(GONE_CHECKS):
        if await cloud.get(droplet_id) is None:
            return True
        if attempt < GONE_CHECKS - 1:
            await clock.sleep(GONE_POLL_SECONDS)
    return False


def status(root: Path) -> dict:
    return {n: {"id": r.droplet_id, "status": r.status} for n, r in load_nodes(root).items()}


def _command(args) -> int:
    from .cli import _cloud
    from .fanout import OpenSSHRemote

    root = Path(args.root)
    if args.fleet_action == "status":
        print(json.dumps(status(root), indent=2, sort_keys=True))
        return 0
    if args.fleet_action == "retire" and not args.name:
        raise FleetError("retire needs --name")
    config = load_config(root)

    async def main() -> dict:
        cloud = _cloud(config.account)
        try:
            if args.fleet_action == "retire":
                return await retire(root, args.name, cloud)
            from ..orchestrator.nodes import issue_token

            remote = OpenSSHRemote(config.identity, root / ".runtime", 0)
            return await reconcile(root, cloud, remote, issue_token)
        finally:
            await cloud.close()

    report = asyncio.run(main())
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.fleet_action == "retire":
        return 0 if report["gone"] else 1
    return 1 if report["failed"] or report["destroy_refused"] else 0


def register(subcommands, handlers: dict) -> None:
    fleet = subcommands.add_parser(
        "pulse-fleet", help="Always-on pulse nodes: reconcile (create only), retire, status"
    )
    fleet.add_argument("fleet_action", choices=["reconcile", "retire", "status"])
    fleet.add_argument("--root", required=True, help="folder holding fleet.json and nodes.json")
    fleet.add_argument("--name", help="the node to retire, atlas-pulse-<region>-<network>")
    handlers["pulse-fleet"] = _command
