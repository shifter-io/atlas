"""Frozen, secret-free campaign definitions shared by all eight runners."""

from __future__ import annotations

import math
import re
import tomllib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .. import __version__
from ..models import CENSUS_RETRY_469_RULE_VERSION, CENSUS_RULE_VERSION
from ..providers import registry
from ..regions import REGION_IDS, load_countries, load_regions
from ..targets import registry as targets
from .files import atomic_json, digest, private_dir, read_json

DEFAULTS = {
    "concurrency": 1000,
    "processes": 0,
    "timeout_seconds": 10.0,
    "min_sample": 1000,
    "stall_seconds": 600.0,
}
# Ephemeral (version 3) workers run one round each on a fresh node, at a higher default.
EPHEMERAL_CONCURRENCY = 2000
GRACE_HOURS = (1, 168)
# Ephemeral (version 3) round spacing in minutes; 60 (hourly) is the default and is not written.
SLOT_MINUTES = (5, 6, 10, 12, 15, 20, 30, 60)


def timestamp(value: float | datetime) -> str:
    moment = datetime.fromtimestamp(value, UTC) if isinstance(value, (int, float)) else value
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def moment(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("Timestamps must have an explicit UTC offset (Z or +00:00)")
    return parsed.astimezone(UTC)


def slug(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,95}", value):
        raise ValueError("Identifiers must use lowercase letters, digits, hyphens or underscores")
    return value


def settings(value: dict) -> dict:
    value = {"processes": 0} | value
    retry = value.get("retry_http_469", False)
    if type(retry) is not bool:
        raise ValueError("retry_http_469 must be a boolean")
    if type(value["processes"]) is not int or value["processes"] < 0:
        raise ValueError("processes must be a nonnegative integer")
    if set(value) - {"retry_http_469"} != set(DEFAULTS):
        raise ValueError("Invalid onboarding settings")
    for key in ("concurrency", "min_sample"):
        if type(value[key]) is not int or value[key] <= 0:
            raise ValueError(f"{key} must be a positive integer")
    for key in ("timeout_seconds", "stall_seconds"):
        number = value[key]
        if type(number) not in (float, int) or not math.isfinite(number) or number <= 0:
            raise ValueError(f"{key} must be finite and positive")
    return dict(value)


@dataclass(frozen=True, slots=True)
class PlanOptions:
    """The keyword options of :func:`create`; unknown names raise ``TypeError``."""

    execution_mode: str = "parallel"
    region_order: list[str] | None = None
    retry_http_469: bool = False
    target_url: str | None = None
    run_settings: dict | None = None
    grace_hours: int = 48
    allow_current_hour: bool = False
    slot_minutes: int = 60


def _start(start: str, now: datetime | None, plan: PlanOptions) -> datetime:
    if plan.execution_mode not in ("parallel", "sequential", "ephemeral"):
        raise ValueError("Execution mode must be parallel, sequential or ephemeral")
    if plan.allow_current_hour and plan.execution_mode != "ephemeral":
        raise ValueError("Only ephemeral campaigns may start in the current hour")
    begins = moment(start)
    if begins.minute or begins.second or begins.microsecond:
        raise ValueError("Campaign start must be a whole UTC hour")
    current = now or datetime.now(UTC)
    if begins <= current and not (plan.allow_current_hour
                                  and current < begins + timedelta(hours=1)):
        raise ValueError("Campaign start must be in the future")
    return begins


def _run_settings(plan: PlanOptions) -> dict:
    base = DEFAULTS | ({"concurrency": EPHEMERAL_CONCURRENCY}
                       if plan.execution_mode == "ephemeral" else {})
    effective = settings(base | (plan.run_settings or {}))
    if type(plan.retry_http_469) is not bool:
        raise ValueError("retry_http_469 must be a boolean")
    if plan.retry_http_469:
        effective["retry_http_469"] = True
    elif effective.get("retry_http_469"):
        raise ValueError("Enable retry_http_469 explicitly")
    return effective


def create(
    provider_config: Path, product: str, start: str, output: Path, *, now: datetime | None = None,
    **options,
) -> Path:
    """Write a frozen campaign manifest; ``options`` are the :class:`PlanOptions` fields.

    ``ephemeral`` (version 3) campaigns run each hourly round on its own fresh node;
    ``grace_hours`` (1-168) extends ``ends_at`` past the last round as an observation-validity
    bound, never a stop. ``slot_minutes`` (ephemeral only, a divisor of 60) spaces the 24
    rounds that many minutes apart instead of one hour. ``allow_current_hour`` (ephemeral
    only) accepts the current whole UTC hour as the start; past hours are always refused.
    """
    plan = PlanOptions(**options)
    begins = _start(start, now, plan)
    if plan.slot_minutes != 60 and plan.execution_mode != "ephemeral":
        raise ValueError("Only ephemeral campaigns may use another slot interval")
    with provider_config.open("rb") as stream:
        config = tomllib.load(stream)
    provider = slug(config["provider"])
    if config.get("schema_version") != 1 or provider not in registry.all():
        raise ValueError("A registered provider adapter and version 1 configuration are required")
    campaign_id = f"{begins.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}".lower()
    mapping = load_regions()
    order = plan.region_order if plan.region_order is not None else [
        r.slug for r in mapping.regions]
    if (not isinstance(order, list) or len(order) != len(REGION_IDS)
            or set(order) != set(REGION_IDS)):
        raise ValueError("Region order must contain each region exactly once")
    retry = plan.retry_http_469
    manifest = {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "provider": provider,
        "product": slug(product),
        "target": "generic_http",
        "target_url": plan.target_url or targets.get("generic_http").url,
        "starts_at": timestamp(begins),
        "ends_at": timestamp(begins + timedelta(hours=24)),
        "rounds": [timestamp(begins + timedelta(hours=i)) for i in range(24)],
        "regions": {region.slug: list(region.countries) for region in mapping.regions},
        "settings": _run_settings(plan),
        "provider_config_sha256": digest(provider_config),
        "framework_version": __version__,
        "census_rule_version": CENSUS_RETRY_469_RULE_VERSION if retry else CENSUS_RULE_VERSION,
        "census_ratio_basis": ("completed_requests_excluding_http_469" if retry
                               else "completed_requests"),
    }
    if plan.execution_mode == "sequential":
        manifest.update(schema_version=2, execution_mode="sequential", region_order=order,
                        ends_at=timestamp(begins + timedelta(days=len(order))))
    elif plan.execution_mode == "ephemeral":
        manifest.update(schema_version=3, execution_mode="ephemeral",
                        grace_hours=plan.grace_hours)
        if plan.slot_minutes != 60:
            manifest["slot_minutes"] = plan.slot_minutes
        slot = timedelta(minutes=slot_minutes(manifest))
        manifest["rounds"] = [timestamp(begins + i * slot) for i in range(24)]
        manifest["ends_at"] = timestamp(begins + 24 * slot
                                        + timedelta(hours=ephemeral_grace(manifest)))
    validate(manifest)
    directory = output / provider / product / campaign_id
    private_dir(directory.parent)
    directory.mkdir(mode=0o700, exist_ok=False)
    path = directory / "campaign.json"
    atomic_json(path, manifest)
    return path


def load(path: Path) -> dict:
    return validate(read_json(path))


def regional_window(manifest: dict, region: str) -> dict:
    """A region always owns 24 hourly slots, even in an eight-day campaign."""
    if region not in manifest["regions"]:
        raise ValueError("Unknown campaign region")
    if manifest.get("execution_mode") == "ephemeral":
        # Every region shares the campaign window, including its grace bound.
        return {"starts_at": manifest["starts_at"], "ends_at": manifest["ends_at"],
                "rounds": list(manifest["rounds"])}
    offset = (manifest["region_order"].index(region)
              if manifest.get("execution_mode", "parallel") == "sequential" else 0)
    start = moment(manifest["starts_at"]) + timedelta(days=offset)
    return {"starts_at": timestamp(start), "ends_at": timestamp(start + timedelta(days=1)),
            "rounds": [timestamp(start + timedelta(hours=i)) for i in range(24)]}


def validate(data: dict) -> dict:
    retry = data.get("settings", {}).get("retry_http_469", False)
    version = CENSUS_RETRY_469_RULE_VERSION if retry else CENSUS_RULE_VERSION
    basis = "completed_requests_excluding_http_469" if retry else "completed_requests"
    if (
        data.get("schema_version") not in (1, 2, 3)
        or data.get("target") != "generic_http"
        or data.get("census_rule_version") != version
        or data.get("census_ratio_basis") != basis
    ):
        raise ValueError("Unsupported onboarding manifest or census rule")
    for key in ("campaign_id", "provider", "product"):
        slug(data[key])
    if not re.fullmatch(r"[0-9a-f]{64}", data["provider_config_sha256"]):
        raise ValueError("Invalid provider configuration checksum")
    mode = data.get("execution_mode", "parallel")
    if (data["schema_version"] == 1 and (mode != "parallel" or "region_order" in data)):
        raise ValueError("Sequential schedules require manifest version 2")
    if data["schema_version"] == 2:
        order = data.get("region_order")
        if (mode != "sequential" or not isinstance(order, list)
                or len(order) != len(REGION_IDS) or set(order) != set(REGION_IDS)):
            raise ValueError("Sequential schedule requires each region exactly once")
    start, end = moment(data["starts_at"]), moment(data["ends_at"])
    slot = timedelta(minutes=slot_minutes(data))  # one hour but in an ephemeral manifest
    span = 24 * slot * (len(REGION_IDS) if mode == "sequential" else 1)
    span += timedelta(hours=ephemeral_grace(data) if data["schema_version"] == 3 else 0)
    if start.minute or start.second or start.microsecond or end - start != span:
        raise ValueError("Campaign duration must give each region exactly 24 whole UTC hours")
    if data["rounds"] != [timestamp(start + i * slot) for i in range(24)]:
        raise ValueError("Campaign must contain exactly 24 hourly rounds")
    regions = data["regions"]
    if not isinstance(regions, dict) or set(regions) != set(REGION_IDS):
        raise ValueError("Campaign must contain all eight regions")
    codes = []
    for countries in regions.values():
        if not isinstance(countries, list) or not countries:
            raise ValueError("Each region requires an ordered country list")
        if any(not isinstance(code, str) for code in countries):
            raise ValueError("Country codes must be strings")
        codes.extend(countries)
    if len(codes) != len(set(codes)) or set(codes) != set(load_countries()):
        raise ValueError("Each configured country must occur exactly once")
    settings(data["settings"])
    # Validate custom destinations using the same constraints as the request runner.
    from ..models import ProxyConfig, RunConfig

    RunConfig("generic_http", ProxyConfig("http", "localhost", 8080),
              target_url=data["target_url"])
    return data


def slot_minutes(data: dict) -> int:
    """Minutes between rounds: 60 unless an ephemeral manifest records `slot_minutes`."""
    if "slot_minutes" not in data:
        return 60
    value = data["slot_minutes"]
    if data.get("schema_version") != 3 or type(value) is not int or value not in SLOT_MINUTES[:-1]:
        raise ValueError("slot_minutes must be a divisor of 60 below 60, ephemeral only")
    return value


def ephemeral_grace(data: dict) -> int:
    """Validate the version 3 fields and return the grace bound in hours."""
    grace = data.get("grace_hours")
    if data.get("execution_mode") != "ephemeral" or "region_order" in data:
        raise ValueError("Manifest version 3 requires the ephemeral execution mode")
    if type(grace) is not int or not GRACE_HOURS[0] <= grace <= GRACE_HOURS[1]:
        raise ValueError("grace_hours must be an integer from 1 to 168")
    return grace
