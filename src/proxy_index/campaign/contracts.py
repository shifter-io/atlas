"""Frozen campaign contracts: ownership markers, accounts, records, protocols and paths.

Changing this module is its own reviewed task, merged first and re-run with the scope suite.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from ..onboarding.manifest import SLOT_MINUTES
from ..regions import REGION_IDS

WORKER_TAG = "pi-camp-worker"
MANAGER_TAG = "pi-camp-manager"
BUILDER_TAG = "pi-camp-builder"
RESOURCE_PREFIX = "pi-camp-"
WORKER_NAME_RE = r"^pic-[0-9a-f]{8}-h[0-9]{2}-[a-z0-9]+-[a-z0-9-]+$"
MANAGER_NAME_RE = r"^pi-camp-manager-[0-9]{8}-[0-9a-f]{4}$"
BUILDER_NAME_RE = r"^pi-camp-builder-[0-9]{8}-[0-9a-f]{4}$"
# Droplet role -> (role tag, name pattern); a droplet carries exactly one role tag.
ROLES = {
    "worker": (WORKER_TAG, WORKER_NAME_RE),
    "manager": (MANAGER_TAG, MANAGER_NAME_RE),
    "builder": (BUILDER_TAG, BUILDER_NAME_RE),
}
CAMPAIGN_ID_RE = r"^[0-9]{8}t[0-9]{6}z-[0-9a-f]{12}$"
NETWORK_RE = r"^[a-z0-9][a-z0-9-]*$"
# CPU-Optimized (c-2, c2-4, c-4-intel) or Basic (s-2vcpu-4gb, s-4vcpu-8gb-amd) droplet slugs.
SIZE_RE = r"c2?-\d+(-intel)?|s-\d+vcpu-\d+gb(-amd|-intel)?"
WORKER_ROOT = "/opt/proxy-index-worker"
WORKER_SECRETS = "/run/proxy-index-worker"

HourState = Literal["planned", "launching", "running", "harvesting", "done", "failed"]
CellStatus = Literal["pending", "running", "complete", "partial", "failed_ours", "paused"]
DropletStatus = Literal[
    "requested",
    "create_failed",
    "assign_failed",
    "assigned",
    "booting",
    "boot_failed",
    "starting",
    "start_failed",
    "armed",
    "running",
    "finished",
    "collected",
    "collect_failed",
    "destroyed",
    "destroy_refused",
    "destroy_failed",
]


def short_id(cid: str) -> str:
    """The 8 hex characters of the campaign id's random part (`<yyyymmddThhmmssZ>-<hex12>`)."""
    if not re.fullmatch(CAMPAIGN_ID_RE, cid):
        raise ValueError("campaign id must look like 20270101t220000z-0123456789ab")
    return cid.split("-")[1][:8]


def campaign_tag(cid: str) -> str:
    return f"{RESOURCE_PREFIX}{short_id(cid)}"


def hour_tag(cid: str, h: int) -> str:
    return f"{campaign_tag(cid)}-h{h:02d}"


def droplet_name(cid: str, h: int, region: str, network: str) -> str:
    name = f"pic-{short_id(cid)}-h{h:02d}-{region}-{network}"
    if not re.fullmatch(WORKER_NAME_RE, name):
        raise ValueError("region or network does not fit the worker name pattern")
    return name


@dataclass(frozen=True)
class Account:
    key: str
    keychain_service: str
    keychain_account: str
    project_name: str


PROJECT_NAME = "Proxy Index"  # every account keeps campaign resources in this project
ACCOUNTS = {
    "leftclick": Account("leftclick", "Leftclick DigitalOcean", "team: Leftclick", PROJECT_NAME),
    "proxy-index": Account(
        "proxy-index", "Proxy Index DigitalOcean", "team: Proxy Index", PROJECT_NAME
    ),
}


def _whole_hour(value: str) -> bool:
    moment = datetime.fromisoformat(value)
    return moment.utcoffset() is not None and moment.minute == moment.second == 0


@dataclass(frozen=True)
class CampaignSpec:
    """The plan for one campaign: identity, slots, scope, machines, timing and mode."""

    campaign_id: str
    created_at: str
    account: str
    project_id: str
    starts_at: str
    grace_hours: int
    regions: tuple[str, ...]
    networks: tuple[str, ...]
    manifests: dict[str, str]
    size_by_region: dict[str, str]
    image_by_region: dict[str, str]
    image_name: str
    wheel_sha256: str
    framework_version: str
    ssh_key_id: str
    manager_droplet_id: str
    create_lead_seconds: int = 90
    replace_until_minutes: int = 15
    synthetic: bool = False
    concurrency: int = 2000
    hours: int = 24  # the number of slots (24 for a campaign; fewer for a compressed test)
    schema_version: int = 1
    slot_minutes: int = 60  # slot k is scheduled at starts_at + k * slot_minutes

    def validate(self) -> CampaignSpec:
        short_id(self.campaign_id)
        order = [region for region in REGION_IDS if region in self.regions]
        problems = [
            (self.schema_version != 1, "schema_version"),
            (self.account not in ACCOUNTS, "account"),
            (not self.project_id or not self.manager_droplet_id or not self.ssh_key_id, "ids"),
            (not _whole_hour(self.starts_at), "starts_at"),
            (datetime.fromisoformat(self.created_at).utcoffset() is None, "created_at"),
            (not 1 <= self.hours <= 24 or not 1 <= self.grace_hours <= 168, "hours"),
            (self.slot_minutes not in SLOT_MINUTES, "slot_minutes"),
            (not self.regions or list(self.regions) != order, "regions"),
            (not self.networks or len(set(self.networks)) != len(self.networks), "networks"),
            (any(not re.fullmatch(NETWORK_RE, n) for n in self.networks), "network names"),
            (set(self.manifests) != set(self.networks), "manifests"),
            (
                set(self.size_by_region) != set(self.regions)
                or any(
                    not isinstance(size, str) or not re.fullmatch(SIZE_RE, size)
                    for size in self.size_by_region.values()
                ),
                "size_by_region",
            ),
            (set(self.image_by_region) != set(self.regions), "image_by_region"),
            (not re.fullmatch(r"[0-9a-f]{64}", self.wheel_sha256), "wheel_sha256"),
            (
                self.create_lead_seconds <= 0
                or self.replace_until_minutes < 0
                or self.create_lead_seconds >= self.slot_minutes * 60
                or self.replace_until_minutes > self.slot_minutes,
                "timing",
            ),
            (not 1 <= self.concurrency <= 5000, "concurrency"),
        ]
        integers = (
            "hours",
            "grace_hours",
            "create_lead_seconds",
            "replace_until_minutes",
            "concurrency",
            "schema_version",
            "slot_minutes",
        )
        problems += [(type(getattr(self, name)) is not int, name) for name in integers]
        bad = [name for failed, name in problems if failed]
        if bad:
            raise ValueError(f"invalid campaign spec: {', '.join(bad)}")
        return self

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True, allow_nan=False) + "\n"

    @classmethod
    def from_json(cls, text: str) -> CampaignSpec:
        raw = json.loads(text)
        known = {item.name for item in fields(cls)}
        if not isinstance(raw, dict) or set(raw) - known:
            raise ValueError("unknown campaign spec fields")
        raw["regions"] = tuple(raw.get("regions", ()))
        raw["networks"] = tuple(raw.get("networks", ()))
        return cls(**raw).validate()


@dataclass
class DropletRecord:
    """One worker's life in the ledger. Never holds an IP address."""

    network: str
    region: str
    hour: int
    name: str
    droplet_id: str | None = None
    size: str = ""
    image_id: str = ""
    status: DropletStatus = "requested"
    attempts: int = 0
    requested_at: str | None = None
    assigned_at: str | None = None
    active_at: str | None = None
    ssh_ready_at: str | None = None
    armed_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    collected_at: str | None = None
    destroyed_at: str | None = None
    start_lateness_seconds: float | None = None
    overlap: int = 0
    error_class: str | None = None
    exit_code: int | None = None
    requests: int = 0
    successes: int = 0
    errors: int = 0
    bytes_up: int = 0
    bytes_down: int = 0
    cpu_peak_pct: float | None = None
    rss_peak_mb: float | None = None
    concurrency: int | None = None
    cpu_p50_pct: float | None = None
    cpu_p95_pct: float | None = None
    cpu_max_pct: float | None = None
    cpu_core_max_pct: float | None = None
    steal_p95_pct: float | None = None
    load1_p95: float | None = None
    mem_used_max_pct: float | None = None
    # Written only by scope.assign_and_verify_project: this id came from our own create and its
    # membership in Proxy Index was never confirmed. The own-unassigned proof across processes.
    assign_failed_at: str | None = None
    # A replacement: the index, in its hour's `droplets`, of the worker it replaces.
    replaced_from: int | None = None
    # Why it failed, for diagnosis (`scope.error_detail`): exception class, message without
    # addresses or tokens, failed checks and the HTTP statuses of the calls about it.
    error_detail: dict[str, Any] | None = None


@dataclass
class HourRecord:
    campaign_id: str
    hour: int
    scheduled_at: str
    state: HourState = "planned"
    fired_at: str | None = None
    launched_at: str | None = None
    finished_at: str | None = None
    launch_delay_seconds: float | None = None
    droplets: list[DropletRecord] = field(default_factory=list)
    cells: dict[str, CellStatus] = field(default_factory=dict)
    traffic: dict[str, dict[str, float]] = field(default_factory=dict)
    report_path: str | None = None
    alerts: list[dict[str, Any]] = field(default_factory=list)
    # Per replaced cell: `replaced_from` (each failed worker's droplet id, status and error
    # class) and `extra_delay_seconds` (how far past HH:00 the replacement armed, else None).
    replacements: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Networks the owner had paused when the tick launched this hour; their cells without a
    # worker are final `paused` cells. Empty in records written before the field existed.
    paused_networks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> HourRecord:
        values = dict(raw)
        values["droplets"] = [DropletRecord(**item) for item in raw.get("droplets", [])]
        return cls(**values)


@dataclass(frozen=True)
class Droplet:
    """A droplet as the API reports it; `public_ipv4` stays in memory and `.runtime` only."""

    id: str
    name: str
    region: str
    size: str
    status: str
    created_at: datetime
    tags: tuple[str, ...]
    public_ipv4: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Scope:
    """What one campaign owns: its short id, project and recorded manager."""

    campaign_id: str
    sid: str
    project_id: str
    manager_droplet_id: str
    created_at: datetime


class Cloud(Protocol):
    """Read and create operations only. Deletes exist solely in `scope.guarded_delete`."""

    async def create(
        self,
        names: list[str],
        *,
        region: str,
        size: str,
        image: str,
        tags: list[str],
        ssh_keys: list[str],
        **flags: bool,
    ) -> list[Droplet]: ...
    async def assign_project(
        self, project_id: str, droplet_ids: list[str], *, guard: object
    ) -> None: ...
    async def get(self, droplet_id: str) -> Droplet | None: ...
    async def list_tag(self, tag: str) -> list[Droplet]: ...
    async def project_droplet_ids(self, project_id: str) -> frozenset[str]: ...
    async def account(self) -> dict[str, Any]: ...


class Remote(Protocol):
    async def wait_ready(self, droplet: Droplet, limit_seconds: float) -> bool: ...
    async def push_start(self, droplet: Droplet, payload: bytes, command: list[str]) -> None: ...
    async def inspect(self, droplet: Droplet) -> dict[str, Any]: ...
    async def stop(self, droplet: Droplet) -> None: ...
    async def pull(self, droplet: Droplet, remote: str, local: Path) -> None: ...
    async def journal(self, droplet: Droplet, local: Path) -> None: ...


HourLauncher = Callable[[CampaignSpec, HourRecord, Path], Awaitable[HourRecord]]
Harvester = Callable[[CampaignSpec, HourRecord, Path], Awaitable[HourRecord]]


def spec_path(root: Path) -> Path:
    return root / "campaign-spec.json"


def hour_path(root: Path, h: int) -> Path:
    return root / "ledger" / f"{h:02d}" / "hour.json"


def audit_path(root: Path) -> Path:
    return root / "ledger" / "delete-audit.jsonl"


def network_root(root: Path, net: str, mcid: str) -> Path:
    return root / "networks" / net / "residential" / mcid


def runtime_dir(root: Path) -> Path:
    return root / ".runtime"


def logs_dir(root: Path, h: int) -> Path:
    return root / "logs" / f"{h:02d}"


def reports_dir(root: Path) -> Path:
    return root / "reports"


def status_path(root: Path) -> Path:
    return root / "status.json"


def alerts_path(root: Path) -> Path:
    return root / "alerts.jsonl"
