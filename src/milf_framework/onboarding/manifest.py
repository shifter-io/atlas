"""Frozen, secret-free campaign definitions shared by all eight runners."""

from __future__ import annotations

import math
import re
import tomllib
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .. import __version__
from ..models import CENSUS_RULE_VERSION
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
    if type(value["processes"]) is not int or value["processes"] < 0:
        raise ValueError("processes must be a nonnegative integer")
    if set(value) != set(DEFAULTS):
        raise ValueError("Invalid onboarding settings")
    for key in ("concurrency", "min_sample"):
        if type(value[key]) is not int or value[key] <= 0:
            raise ValueError(f"{key} must be a positive integer")
    for key in ("timeout_seconds", "stall_seconds"):
        number = value[key]
        if type(number) not in (float, int) or not math.isfinite(number) or number <= 0:
            raise ValueError(f"{key} must be finite and positive")
    return dict(value)


def create(
    provider_config: Path, product: str, start: str, output: Path, *, now: datetime | None = None
) -> Path:
    begins = moment(start)
    if begins.minute or begins.second or begins.microsecond:
        raise ValueError("Campaign start must be a whole UTC hour")
    if begins <= (now or datetime.now(UTC)):
        raise ValueError("Campaign start must be in the future")
    with provider_config.open("rb") as stream:
        config = tomllib.load(stream)
    provider = slug(config["provider"])
    if config.get("schema_version") != 1 or provider not in registry.all():
        raise ValueError("A registered provider adapter and version 1 configuration are required")
    campaign_id = f"{begins.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}".lower()
    mapping = load_regions()
    manifest = {
        "schema_version": 1,
        "campaign_id": campaign_id,
        "provider": provider,
        "product": slug(product),
        "target": "generic_http",
        "target_url": targets.get("generic_http").url,
        "starts_at": timestamp(begins),
        "ends_at": timestamp(begins + timedelta(hours=24)),
        "rounds": [timestamp(begins + timedelta(hours=i)) for i in range(24)],
        "regions": {region.slug: list(region.countries) for region in mapping.regions},
        "settings": dict(DEFAULTS),
        "provider_config_sha256": digest(provider_config),
        "framework_version": __version__,
        "census_rule_version": CENSUS_RULE_VERSION,
        "census_ratio_basis": "completed_requests",
    }
    directory = output / provider / product / campaign_id
    private_dir(directory.parent)
    directory.mkdir(mode=0o700, exist_ok=False)
    path = directory / "campaign.json"
    atomic_json(path, manifest)
    return path


def load(path: Path) -> dict:
    data = read_json(path)
    if (
        data.get("schema_version") != 1
        or data.get("target") != "generic_http"
        or data.get("census_rule_version") != CENSUS_RULE_VERSION
        or data.get("census_ratio_basis") != "completed_requests"
    ):
        raise ValueError("Unsupported onboarding manifest or census rule")
    for key in ("campaign_id", "provider", "product"):
        slug(data[key])
    if not re.fullmatch(r"[0-9a-f]{64}", data["provider_config_sha256"]):
        raise ValueError("Invalid provider configuration checksum")
    start, end = moment(data["starts_at"]), moment(data["ends_at"])
    if start.minute or start.second or start.microsecond or end - start != timedelta(hours=24):
        raise ValueError("Campaign must cover exactly 24 whole UTC hours")
    if data["rounds"] != [timestamp(start + timedelta(hours=i)) for i in range(24)]:
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
    return data
