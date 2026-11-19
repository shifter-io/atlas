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
from typing import Any, Literal

from .. import legacy_names
from ..onboarding.manifest import SLOT_MINUTES
from ..regions import REGION_IDS
from .cloud_types import PRODUCTION, Cloud, Droplet, Remote, Scope  # noqa: F401 (re-exported)
from .sizes import SIZE_RE, size_list  # noqa: F401 (re-exported)

WORKER_TAG = "atlas-camp-worker"
MANAGER_TAG = "atlas-camp-manager"
BUILDER_TAG = "atlas-camp-builder"
RESOURCE_PREFIX = "atlas-camp-"
WORKER_NAME_RE = r"^atlas-[0-9a-f]{8}-h[0-9]{2}-[a-z0-9]+-[a-z0-9-]+$"
MANAGER_NAME_RE = r"^atlas-camp-manager-[0-9]{8}-[0-9a-f]{4}$"
BUILDER_NAME_RE = r"^atlas-camp-builder-[0-9]{8}-[0-9a-f]{4}$"
# Always-on pulse nodes: never a campaign's, kept in project Dev Resources; only
# scope.retire_pulse_node removes one.
PULSE_TAG = "atlas-pulse-node"
PULSE_PREFIX = "atlas-pulse-"
PULSE_NAME_RE = r"^atlas-pulse-[a-z0-9]+-[a-z0-9-]+$"
PULSE_PROJECT_NAME = "Dev Resources"
# Droplet role -> (role tag, name pattern); a droplet carries exactly one role tag.
CAMPAIGN_ROLES = {
    "worker": (WORKER_TAG, WORKER_NAME_RE),
    "manager": (MANAGER_TAG, MANAGER_NAME_RE),
    "builder": (BUILDER_TAG, BUILDER_NAME_RE),
}
PULSE_ROLE = (PULSE_TAG, PULSE_NAME_RE)
ROLES = {**CAMPAIGN_ROLES, "pulse": PULSE_ROLE}
# CAMP-STAGING-1: a staging manager runs beside production on the same account. Its droplets,
# firewalls and keys carry the atlas-camp-stg- prefix; worker names and campaign tags stay the
# campaign's (its short id), and the worker image is production's, read only.
STAGING = "staging"  # PRODUCTION lives in cloud_types, the default of Scope
STAGING_PREFIX = "atlas-camp-stg-"
STAGING_MANAGER_NAME_RE = r"^atlas-camp-stg-manager-[0-9]{8}-[0-9a-f]{4}$"
STAGING_ROLES = {
    "worker": ("atlas-camp-stg-worker", WORKER_NAME_RE),
    "manager": ("atlas-camp-stg-manager", STAGING_MANAGER_NAME_RE),
    "builder": ("atlas-camp-stg-builder", r"^atlas-camp-stg-builder-[0-9]{8}-[0-9a-f]{4}$"),
}
# Environment -> (resource prefix, role -> (role tag, name pattern)).
ENVIRONMENTS = {
    PRODUCTION: (RESOURCE_PREFIX, CAMPAIGN_ROLES),
    STAGING: (STAGING_PREFIX, STAGING_ROLES),
}
# Every role of every environment, and the pulse role: a droplet carries exactly one role tag.
ALL_ROLES = (
    *(role for _, table in ENVIRONMENTS.values() for role in table.values()),
    PULSE_ROLE,
)
ALL_ROLE_TAGS = frozenset(tag for tag, _ in ALL_ROLES)
CAMPAIGN_ID_RE = r"^[0-9]{8}t[0-9]{6}z-[0-9a-f]{12}$"
NETWORK_RE = r"^[a-z0-9][a-z0-9-]*$"
WORKER_ROOT = "/opt/atlas-worker"
WORKER_SECRETS = "/run/atlas-worker"

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


def check_environment(value: object) -> str:
    if value not in ENVIRONMENTS:
        raise ValueError(f"environment must be one of {', '.join(ENVIRONMENTS)}")
    return str(value)


def environment_of(name: str) -> str | None:
    """The environment whose resource prefix `name` carries (staging's is the longer), else
    None. A campaign tag (`atlas-camp-<short id>`) reads as production's; a name from before
    the rename (legacy_names) reads as its current one."""
    name = legacy_names.current(name)
    if name.startswith(STAGING_PREFIX):
        return STAGING
    return PRODUCTION if name.startswith(RESOURCE_PREFIX) else None


def with_legacy(names: list[str]) -> list[str]:
    """`names` and, after them, each one's pre-rename spelling (RENAME-ATLAS-2): what a count or
    a cleanup lists so that resources created before the rename are not orphaned."""
    return [*names, *filter(None, map(legacy_names.old, names))]


def roles(environment: str) -> dict[str, tuple[str, str]]:
    """Role -> (role tag, name pattern) of one environment, with the pulse role."""
    return ROLES | ENVIRONMENTS[check_environment(environment)][1]


def tags(environment: str) -> dict[str, str]:
    """Role -> role tag of one environment's campaign droplets (worker, manager, builder)."""
    found = ENVIRONMENTS[check_environment(environment)][1]
    return {role: tag for role, (tag, _) in found.items()}


@dataclass(frozen=True)
class Names:
    """One environment's resource names: its firewalls, the manager and dated SSH keys."""

    prefix: str

    def firewall(self, role: str) -> str:
        return f"{self.prefix}{role}-fw"

    def dated(self, role: str, day: datetime, suffix: str = "") -> str:
        """`<prefix><role>-<yyyymmdd>[-<suffix>]`: the manager (role "manager", a random
        suffix) or an SSH key (role "mac" or "worker")."""
        return f"{self.prefix}{role}-{day:%Y%m%d}" + (f"-{suffix}" if suffix else "")


def names(environment: str) -> Names:
    return Names(ENVIRONMENTS[check_environment(environment)][0])


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
    name = f"atlas-{short_id(cid)}-h{h:02d}-{region}-{network}"
    if not re.fullmatch(WORKER_NAME_RE, name):
        raise ValueError("region or network does not fit the worker name pattern")
    return name


@dataclass(frozen=True)
class Account:
    key: str
    keychain_service: str
    keychain_account: str
    project_name: str


PROJECT_NAME = "Atlas"  # every account keeps campaign resources in this project
# The project's pre-rename name, accepted for one release (legacy_names).
PROJECT_NAMES = (PROJECT_NAME, legacy_names.PROJECT_NAME)
ACCOUNTS = {
    "leftclick": Account("leftclick", "Leftclick DigitalOcean", "team: Leftclick", PROJECT_NAME),
    "atlas": Account("atlas", "Atlas DigitalOcean", "team: Atlas", PROJECT_NAME),
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
    size_by_region: dict[str, str | list[str]]  # a slug, or slugs in fallback order
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
    environment: str = PRODUCTION  # the manager's; its workers carry that environment's tag

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
                or not all(size_list(sizes) for sizes in self.size_by_region.values()),
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
            (self.environment not in ENVIRONMENTS, "environment"),
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

    def sizes(self, region: str) -> tuple[str, ...]:
        """The region's worker sizes in the order a create tries them."""
        return size_list(self.size_by_region[region])

    def to_json(self) -> str:
        """A production spec is written without `environment`, exactly as before staging."""
        raw = asdict(self)
        if raw["environment"] == PRODUCTION:
            del raw["environment"]
        return json.dumps(raw, indent=2, sort_keys=True, allow_nan=False) + "\n"

    @classmethod
    def from_json(cls, text: str) -> CampaignSpec:
        raw = json.loads(text)
        known = {item.name for item in fields(cls)}
        if not isinstance(raw, dict) or set(raw) - known:
            raise ValueError("unknown campaign spec fields")
        raw["regions"] = tuple(raw.get("regions", ()))
        raw["networks"] = tuple(raw.get("networks", ()))
        raw["account"] = legacy_names.account(str(raw.get("account", "")))
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
    # membership in Atlas was never confirmed. The own-unassigned proof across processes.
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
