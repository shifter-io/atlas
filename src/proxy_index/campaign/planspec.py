"""`campaign plan`: validate every input, then write the v3 manifests and the campaign spec.

Nothing is written until the credentials (`load_provider`, no network I/O, no values printed),
the worker size in every region, the snapshot in every region, the worker key and the project
are all confirmed. The spec binds the deployed manager and wheel from the manager inventory.
"""

from __future__ import annotations

import re
import secrets
import shutil
import tomllib
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from ..onboarding import manifest
from ..onboarding.files import atomic_bytes, read_json
from ..providers import load_provider
from ..regions import REGION_IDS
from .contracts import NETWORK_RE, PROJECT_NAME, CampaignSpec, spec_path
from .do_api import DigitalOcean

ALL16 = ("byteful", "dataimpulse", "decodo", "evomi", "fleetproxy", "infatica", "iproyal",
         "massive", "nodemaven", "oxylabs", "proxy-cheap", "proxy-seller", "proxyempire",
         "rayobyte", "soax", "webshare")
WORKER_SIZE = "c-2"
PRODUCT = "residential"


class PlanError(ValueError):
    """An input the campaign cannot run with; the message names it, never a secret value."""


@dataclass(frozen=True)
class PlanInputs:
    account: str
    start: str  # an ISO whole hour with offset, or "now-hour" (the dry run)
    networks: tuple[str, ...]
    image: str
    output: Path
    credentials: Path
    inventory: Path  # the manager inventory written by `manager deploy`
    images: Path  # the image inventory written by `image build`
    regions: tuple[str, ...] = tuple(REGION_IDS)
    configs: Path = Path("configs")
    ssh_key_name: str | None = None
    concurrency: int = 2000
    grace_hours: int = 48
    synthetic: bool = False


def parse_networks(value: str) -> tuple[str, ...]:
    names = ALL16 if value == "all16" else tuple(n.strip() for n in value.split(",") if n)
    if not names or len(set(names)) != len(names) or any(
            not re.fullmatch(NETWORK_RE, n) for n in names):
        raise PlanError("--networks must be all16 or distinct network names")
    return names


def parse_start(value: str, now: datetime) -> tuple[datetime, bool]:
    """(start, current hour allowed). `now-hour` is the current whole UTC hour."""
    if value == "now-hour":
        return now.astimezone(UTC).replace(minute=0, second=0, microsecond=0), True
    begins = datetime.fromisoformat(value)
    if begins.utcoffset() is None:
        raise PlanError("--start needs an explicit UTC offset")
    return begins.astimezone(UTC), False


def check_credentials(inputs: PlanInputs) -> None:
    """Each network's public config names it and loads with its private credentials."""
    for network in inputs.networks:
        config = inputs.configs / f"{network}.toml"
        try:
            with config.open("rb") as stream:
                provider = tomllib.load(stream).get("provider")
            if provider != network:
                raise ValueError("config names another provider")
            load_provider(config, credentials_file=inputs.credentials)
        except (OSError, ValueError) as error:
            raise PlanError(f"network {network}: {type(error).__name__}: {error}") from None


def image_id(inputs: PlanInputs) -> str:
    found = {str(b["snapshot_id"]) for b in read_json(inputs.images).get("builds", [])
             if b.get("snapshot_name") == inputs.image and b.get("snapshot_id")}
    if len(found) != 1:
        raise PlanError(f"image {inputs.image} is not one recorded build")
    return found.pop()


async def check_cloud(cloud: DigitalOcean, inputs: PlanInputs, key_name: str) -> dict:
    """Read-only: the project id, the size and image per region, and the worker key id."""
    project_id = await cloud.project_id(PROJECT_NAME)
    offered = (await cloud.sizes()).get(WORKER_SIZE, [])
    snapshot = image_id(inputs)
    image = await cloud.image(snapshot) or {}
    keys = [str(k["id"]) for k in await cloud.ssh_keys() if k.get("name") == key_name]
    problems = [f"size {WORKER_SIZE} in {r}" for r in inputs.regions if r not in offered]
    problems += [f"image in {r}" for r in inputs.regions if image.get("name") != inputs.image
                 or r not in image.get("regions", [])]
    problems += [] if len(keys) == 1 else [f"ssh key {key_name}"]
    if problems:
        raise PlanError(f"not available: {', '.join(problems)}")
    return {"project_id": project_id, "image_id": snapshot, "ssh_key_id": keys[0]}


def _manifests(inputs: PlanInputs, begins: datetime, current: bool, root: Path,
               now: datetime) -> dict[str, str]:
    """One v3 manifest per network, with its public run config beside it (SHA-bound)."""
    found = {}
    for network in inputs.networks:
        config = inputs.configs / f"{network}.toml"
        path = manifest.create(
            config, PRODUCT, begins.isoformat(), root / "networks", now=now,
            execution_mode="ephemeral", grace_hours=inputs.grace_hours,
            allow_current_hour=current, run_settings={"concurrency": inputs.concurrency})
        atomic_bytes(path.parent / "provider.toml", config.read_bytes())
        found[network] = path.relative_to(root).as_posix()
    return found


async def plan(cloud: DigitalOcean, inputs: PlanInputs, *, now: datetime | None = None) -> dict:
    """Validate everything, then write `<output>/<campaign id>/`; returns a summary (no IPs)."""
    now = now or datetime.now(UTC)
    begins, current = parse_start(inputs.start, now)
    regions = tuple(r for r in REGION_IDS if r in inputs.regions)
    if set(inputs.regions) - set(regions) or not regions:
        raise PlanError("--regions must be campaign regions")
    manager = read_json(inputs.inventory)
    if not all(manager.get(key) for key in ("manager_id", "wheel_sha256", "framework_version")):
        raise PlanError("deploy the manager before planning (inventory incomplete)")
    check_credentials(inputs)
    key_name = inputs.ssh_key_name or manager.get("worker_key_name", "")
    found = await check_cloud(cloud, inputs, key_name)
    cid = f"{begins:%Y%m%dt%H%M%S}z-{secrets.token_hex(6)}"
    root = inputs.output / cid
    if root.exists():
        raise PlanError("campaign folder already exists")
    spec = CampaignSpec(
        campaign_id=cid, created_at=now.isoformat(), account=inputs.account,
        project_id=found["project_id"], starts_at=begins.isoformat(),
        grace_hours=inputs.grace_hours, regions=regions, networks=inputs.networks,
        manifests=dict.fromkeys(inputs.networks, ""),
        size_by_region=dict.fromkeys(regions, WORKER_SIZE),
        image_by_region=dict.fromkeys(regions, found["image_id"]), image_name=inputs.image,
        wheel_sha256=manager["wheel_sha256"], framework_version=manager["framework_version"],
        ssh_key_id=found["ssh_key_id"], manager_droplet_id=str(manager["manager_id"]),
        synthetic=inputs.synthetic, concurrency=inputs.concurrency).validate()
    try:
        spec = replace(spec, manifests=_manifests(inputs, begins, current, root, now)).validate()
    except Exception:
        shutil.rmtree(root, ignore_errors=True)  # only the folder this plan just made
        raise
    atomic_bytes(spec_path(root), spec.to_json().encode())
    return {"campaign_id": cid, "root": str(root), "starts_at": spec.starts_at,
            "networks": list(spec.networks), "regions": list(regions),
            "concurrency": spec.concurrency, "synthetic": spec.synthetic}
