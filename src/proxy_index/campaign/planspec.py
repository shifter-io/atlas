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
from ..onboarding.manifest import SLOT_MINUTES
from ..providers import load_provider
from ..providers.grammar import load_grammar
from ..regions import REGION_IDS
from .contracts import NETWORK_RE, PRODUCTION, PROJECT_NAME, SIZE_RE, CampaignSpec, spec_path
from .do_api import DigitalOcean

ALL16 = (
    "byteful",
    "dataimpulse",
    "decodo",
    "evomi",
    "fleetproxy",
    "infatica",
    "iproyal",
    "massive",
    "nodemaven",
    "oxylabs",
    "proxy-cheap",
    "proxy-seller",
    "proxyempire",
    "rayobyte",
    "soax",
    "webshare",
)
WORKER_SIZE = "c-2"  # the default worker size
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
    size: str = WORKER_SIZE
    slot_minutes: int = 60  # a divisor of 60: slot k starts k * slot_minutes after the start
    slots: int = 24  # fewer slots make a compressed test (4 slots of 15 minutes: one hour)


def parse_networks(value: str) -> tuple[str, ...]:
    names = tuple(
        n
        for part in value.split(",")
        if part.strip()
        for n in (ALL16 if part.strip() == "all16" else (part.strip(),))
    )
    if (
        not names
        or len(set(names)) != len(names)
        or any(not re.fullmatch(NETWORK_RE, n) for n in names)
    ):
        raise PlanError("--networks must be distinct network names (all16 adds the 16)")
    return names


def parse_start(value: str, now: datetime) -> tuple[datetime, bool]:
    """(start, current hour allowed). `now-hour` is the current whole UTC hour."""
    if value == "now-hour":
        return now.astimezone(UTC).replace(minute=0, second=0, microsecond=0), True
    begins = datetime.fromisoformat(value)
    if begins.utcoffset() is None:
        raise PlanError("--start needs an explicit UTC offset")
    return begins.astimezone(UTC), False


def census_config(configs: Path, network: str) -> Path:
    """The run config a network's census rounds use: `<net>-onboarding.toml` when the network
    has one, else `<net>.toml`."""
    onboarding = configs / f"{network}-onboarding.toml"
    return onboarding if onboarding.is_file() else configs / f"{network}.toml"


def network_concurrency(network: str, requested: int) -> int:
    """The concurrency a network's census runs at: the requested one, capped at the
    `[testing] max_concurrency` its grammar records (measured by `campaign probe`)."""
    ceiling = load_grammar(network).max_concurrency
    return requested if ceiling is None else min(requested, ceiling)


def check_credentials(inputs: PlanInputs) -> None:
    """Each network's census config names it, loads with its private credentials and, when
    the provider has a strict-targeting flag, turns it off (CTRY-1, owner decision 2026-10-25,
    reversing CAMP-1i): the country is still sent, and an answer from another country or a
    no-IP status counts like a duplicate in the runner's answer classes."""
    for network in inputs.networks:
        config = census_config(inputs.configs, network)
        try:
            with config.open("rb") as stream:
                data = tomllib.load(stream)
            if data.get("provider") != network:
                raise ValueError("config names another provider")
            load_provider(config, credentials_file=inputs.credentials)
            # Read from the file: a grammar may drop a false flag it does not render (Shifter).
            strict = (data.get("parameters") or {}).get("strict")
            if "strict" in load_grammar(network).flags and strict is not False:
                # Explicit, so a census never runs on a provider default.
                raise ValueError(f"{config.name} must set strict = false")
        except (OSError, ValueError) as error:
            raise PlanError(f"network {network}: {type(error).__name__}: {error}") from None


def image_id(inputs: PlanInputs) -> str:
    builds = [
        b
        for b in read_json(inputs.images).get("builds", [])
        if b.get("snapshot_name") == inputs.image and b.get("snapshot_id")
    ]
    found = {str(b["snapshot_id"]) for b in builds}
    if len(found) != 1:
        raise PlanError(f"image {inputs.image} is not one recorded build")
    if not inputs.synthetic and not all(b.get("mmdb_sha256") for b in builds):
        # A campaign never runs without the run-time country check (CTRY-2).
        raise PlanError(
            f"image {inputs.image} has no country database; rebuild it with `image build --mmdb`"
        )
    return found.pop()


async def check_cloud(cloud: DigitalOcean, inputs: PlanInputs, key_name: str) -> dict:
    """Read-only: the project id, the size and image per region, and the worker key id."""
    project_id = await cloud.project_id(PROJECT_NAME)
    offered = (await cloud.sizes()).get(inputs.size, [])
    snapshot = image_id(inputs)
    image = await cloud.image(snapshot) or {}
    keys = [str(k["id"]) for k in await cloud.ssh_keys() if k.get("name") == key_name]
    problems = [f"size {inputs.size} in {r}" for r in inputs.regions if r not in offered]
    problems += [
        f"image in {r}"
        for r in inputs.regions
        if image.get("name") != inputs.image or r not in image.get("regions", [])
    ]
    problems += [] if len(keys) == 1 else [f"ssh key {key_name}"]
    if problems:
        raise PlanError(f"not available: {', '.join(problems)}")
    return {"project_id": project_id, "image_id": snapshot, "ssh_key_id": keys[0]}


def _manifests(
    inputs: PlanInputs, begins: datetime, current: bool, root: Path, now: datetime
) -> dict[str, str]:
    """One v3 manifest per network, with its census run config beside it (SHA-bound)."""
    found = {}
    for network in inputs.networks:
        config = census_config(inputs.configs, network)
        path = manifest.create(
            config,
            PRODUCT,
            begins.isoformat(),
            root / "networks",
            now=now,
            execution_mode="ephemeral",
            grace_hours=inputs.grace_hours,
            allow_current_hour=current,
            run_settings={"concurrency": network_concurrency(network, inputs.concurrency)},
            slot_minutes=inputs.slot_minutes,
        )
        if manifest.slot_minutes(manifest.load(path)) != inputs.slot_minutes:
            raise PlanError("a manifest's slot_minutes differs from the campaign spec")
        atomic_bytes(path.parent / "provider.toml", config.read_bytes())
        found[network] = path.relative_to(root).as_posix()
    return found


def checked_regions(inputs: PlanInputs) -> tuple[str, ...]:
    """The planned regions in campaign order, after the region, size and slot checks."""
    regions = tuple(r for r in REGION_IDS if r in inputs.regions)
    if set(inputs.regions) - set(regions) or not regions:
        raise PlanError("--regions must be campaign regions")
    if not re.fullmatch(SIZE_RE, inputs.size):
        raise PlanError("--size must be a CPU-Optimized or Basic droplet slug")
    if inputs.slot_minutes not in SLOT_MINUTES or not 1 <= inputs.slots <= 24:
        raise PlanError(f"--slot-minutes must be one of {SLOT_MINUTES}; --slots 1 to 24")
    return regions


def checked_manager(inputs: PlanInputs) -> dict:
    """The manager inventory: deployed, and holding every network's checked credentials."""
    manager = read_json(inputs.inventory)
    if not all(manager.get(key) for key in ("manager_id", "wheel_sha256", "framework_version")):
        raise PlanError("deploy the manager before planning (inventory incomplete)")
    check_credentials(inputs)
    absent = [n for n in inputs.networks if n not in manager.get("credentials", [])]
    if absent:
        raise PlanError(f"the manager holds no credentials for {', '.join(absent)}; deploy them")
    return manager


async def plan(cloud: DigitalOcean, inputs: PlanInputs, *, now: datetime | None = None) -> dict:
    """Validate everything, then write `<output>/<campaign id>/`; returns a summary (no IPs)."""
    now = now or datetime.now(UTC)
    begins, current = parse_start(inputs.start, now)
    regions = checked_regions(inputs)
    manager = checked_manager(inputs)
    key_name = inputs.ssh_key_name or manager.get("worker_key_name", "")
    found = await check_cloud(cloud, inputs, key_name)
    if (manager.get("account"), manager.get("project_id")) != (inputs.account, found["project_id"]):
        raise PlanError(f"the manager is on account {manager.get('account')}, not {inputs.account}")
    cid = f"{begins:%Y%m%dt%H%M%S}z-{secrets.token_hex(6)}"
    root = inputs.output / cid
    if root.exists():
        raise PlanError("campaign folder already exists")
    spec = CampaignSpec(
        campaign_id=cid,
        created_at=now.isoformat(),
        account=inputs.account,
        project_id=found["project_id"],
        starts_at=begins.isoformat(),
        grace_hours=inputs.grace_hours,
        regions=regions,
        networks=inputs.networks,
        manifests=dict.fromkeys(inputs.networks, ""),
        size_by_region=dict.fromkeys(regions, inputs.size),
        image_by_region=dict.fromkeys(regions, found["image_id"]),
        image_name=inputs.image,
        wheel_sha256=manager["wheel_sha256"],
        framework_version=manager["framework_version"],
        ssh_key_id=found["ssh_key_id"],
        manager_droplet_id=str(manager["manager_id"]),
        synthetic=inputs.synthetic,
        concurrency=inputs.concurrency,
        hours=inputs.slots,
        slot_minutes=inputs.slot_minutes,
        replace_until_minutes=min(15, inputs.slot_minutes),
        environment=manager.get("environment", PRODUCTION),  # CAMP-STAGING-1: the manager's
    ).validate()
    try:
        spec = replace(spec, manifests=_manifests(inputs, begins, current, root, now)).validate()
    except Exception:
        shutil.rmtree(root, ignore_errors=True)  # only the folder this plan just made
        raise
    atomic_bytes(spec_path(root), spec.to_json().encode())
    return summary(spec, root, regions)


def summary(spec: CampaignSpec, root: Path, regions: tuple[str, ...]) -> dict:
    """What `plan` returns: the campaign, its folder and its settings (no IPs)."""
    return {
        "campaign_id": spec.campaign_id,
        "root": str(root),
        "starts_at": spec.starts_at,
        "networks": list(spec.networks),
        "regions": list(regions),
        "concurrency": spec.concurrency,
        "synthetic": spec.synthetic,
        "slot_minutes": spec.slot_minutes,
        "slots": spec.hours,
        "concurrency_by_network": {
            n: network_concurrency(n, spec.concurrency) for n in spec.networks
        },
    }
