"""Runner regions, the countries each one tests, census slots and the scheduled targets.

configs/regions.yaml maps a hostname pattern (a regular expression matched against the whole,
lower-cased host or region name) to the ISO 3166-1 alpha-2 codes that region tests. Every known
country belongs to exactly one region and every pattern names exactly one region.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

# A source checkout keeps them in framework/configs; a wheel ships copies inside the package.
_PACKAGED = Path(__file__).resolve().parent / "configs"
CONFIGS = _PACKAGED if _PACKAGED.is_dir() else Path(__file__).resolve().parents[2] / "configs"
REGIONS_FILE = CONFIGS / "regions.yaml"
COUNTRIES_FILE = CONFIGS / "countries.json"
TARGETS_FILE = CONFIGS / "targets.toml"
# Stable ids (the collector's), so runner_regions keys survive a re-seed.
REGION_IDS = {
    "lon1": 1,
    "fra1": 2,
    "ams3": 3,
    "nyc3": 4,
    "tor1": 5,
    "sgp1": 6,
    "blr1": 7,
    "syd1": 9,
}
CLOUD = "digitalocean"
DAY_MINUTES = 24 * 60
# Regions start their first census a few minutes apart so the orchestrator is not hit at once.
REGION_OFFSET_MINUTES = 3


class _Loader(yaml.BaseLoader):
    """Every scalar stays a string (no YAML 1.1 `NO` → false) and duplicate keys are errors."""


def _mapping(loader, node):
    keys = [loader.construct_object(key) for key, _ in node.value]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate key in regions file")
    return loader.construct_mapping(node)


_Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


@dataclass(frozen=True, slots=True)
class Region:
    region_id: int
    slug: str
    pattern: str
    countries: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RegionMap:
    regions: tuple[Region, ...]
    names: dict[str, str]

    def resolve(self, name_or_host: str) -> str:
        """The region slug whose pattern matches this host or region name."""
        value = name_or_host.strip().lower()
        matches = [r.slug for r in self.regions if re.fullmatch(r.pattern, value)]
        if len(matches) != 1:
            raise ValueError(f"Host {name_or_host!r} matches {len(matches)} regions")
        return matches[0]

    def region_of(self, country_code: str) -> str:
        for region in self.regions:
            if country_code.upper() in region.countries:
                return region.slug
        raise KeyError(country_code)

    def census_times(self) -> dict[str, time]:
        """Placeholder UTC slots: each region's countries spread evenly over the day.

        Countries are taken in code order; with at most 58 per region the slots are at least 24
        minutes apart, so no two countries of one region (one node per provider) share a slot.
        """
        times = {}
        for ordinal, region in enumerate(sorted(self.regions, key=lambda r: r.region_id)):
            codes = sorted(region.countries)
            for index, code in enumerate(codes):
                minute = (ordinal * REGION_OFFSET_MINUTES + index * DAY_MINUTES // len(codes)) % (
                    DAY_MINUTES
                )
                times[code] = time(minute // 60, minute % 60)
        return times


def load_countries(path: str | Path | None = None) -> dict[str, str]:
    names = json.loads(Path(path or COUNTRIES_FILE).read_text())
    if not isinstance(names, dict) or not all(
        isinstance(code, str)
        and re.fullmatch(r"[A-Z]{2}", code)
        and isinstance(name, str)
        and name.strip()
        for code, name in names.items()
    ):
        raise ValueError("Country names must map ISO alpha-2 codes to names")
    return names


def parse_regions(text: str, names: dict[str, str]) -> RegionMap:
    document = yaml.load(text, Loader=_Loader)
    if not isinstance(document, dict) or set(document) != {"mapping"}:
        raise ValueError("Regions file must contain only a mapping")
    mapping = document["mapping"]
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError("Regions mapping must not be empty")
    regions, owner = [], {}
    for pattern, codes in mapping.items():
        try:
            compiled = re.compile(pattern)
        except re.error:
            raise ValueError(f"Invalid region pattern {pattern!r}") from None
        slugs = [slug for slug in REGION_IDS if compiled.fullmatch(slug)]
        if len(slugs) != 1:
            raise ValueError(f"Pattern {pattern!r} must match exactly one region, not {slugs}")
        if not isinstance(codes, list) or not codes:
            raise ValueError(f"Pattern {pattern!r} needs a list of country codes")
        for code in codes:
            if not isinstance(code, str) or code not in names:
                raise ValueError(f"Unknown country code {code!r}")
            if code in owner:
                raise ValueError(f"Country {code} is in both {owner[code]} and {slugs[0]}")
            owner[code] = slugs[0]
        regions.append(Region(REGION_IDS[slugs[0]], slugs[0], pattern, tuple(codes)))
    covered = [r.slug for r in regions]
    if sorted(covered) != sorted(REGION_IDS):
        raise ValueError("Every region needs exactly one pattern")
    if missing := sorted(set(names) - set(owner)):
        raise ValueError(f"Countries without a region: {missing}")
    return RegionMap(tuple(regions), dict(names))


def load_regions(
    path: str | Path | None = None, countries_path: str | Path | None = None
) -> RegionMap:
    return parse_regions(Path(path or REGIONS_FILE).read_text(), load_countries(countries_path))


@lru_cache(maxsize=8)
def _cached(path: str) -> RegionMap:
    return load_regions(path)


def resolve_region(name_or_host: str, path: str | Path | None = None) -> str:
    """Region slug for a host such as ``prod-fra1-shifter`` or a bare slug such as ``fra1``."""
    return _cached(str(Path(path or REGIONS_FILE).resolve())).resolve(name_or_host)


TARGET_DEFAULTS: dict[str, Any] = {
    "label": None,
    "engine": "http",
    "pass_condition": None,
    "timeout_ms": 15000,
    "census": False,
    "census_concurrency": 1000,
    "pulse_enabled": True,
    "pulse_interval_minutes": 30,
    "pulse_requests": 100,
    "pulse_concurrency": 25,
    "sort_order": 0,
}
TARGET_TYPES = {
    "id": str,
    "url": str,
    "label": str,
    "engine": str,
    "pass_condition": str,
    "timeout_ms": int,
    "census": bool,
    "census_concurrency": int,
    "pulse_enabled": bool,
    "pulse_interval_minutes": int,
    "pulse_requests": int,
    "pulse_concurrency": int,
    "sort_order": int,
}
POSITIVE = ("timeout_ms", "census_concurrency", "pulse_interval_minutes", "pulse_requests",
            "pulse_concurrency")


def load_targets(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Validated [[target]] rows with defaults filled in; `id` becomes `target_id`."""
    document = tomllib.loads(Path(path or TARGETS_FILE).read_text())
    if set(document) != {"target"} or not isinstance(document["target"], list):
        raise ValueError("Targets file must contain only [[target]] tables")
    rows, seen = [], set()
    for entry in document["target"]:
        if unknown := set(entry) - set(TARGET_TYPES):
            raise ValueError(f"Unknown target fields {sorted(unknown)}")
        row = TARGET_DEFAULTS | entry
        for key, kind in TARGET_TYPES.items():
            value = row.get(key)
            # A seeded target always has a label; unlabelled rows are the ones ingest added.
            if key == "pass_condition" and value is None:
                continue
            # bool is an int subclass; keep the two apart.
            if type(value) is not kind:
                raise ValueError(f"Target field {key} must be {kind.__name__}")
        if not re.fullmatch(r"[a-z0-9_]+", row["id"]) or row["id"] in seen:
            raise ValueError(f"Invalid or duplicate target id {row['id']!r}")
        if not re.match(r"https?://", row["url"]):
            raise ValueError("Target URL must be http or https")
        if any(row[key] <= 0 for key in POSITIVE):
            raise ValueError("Target timeouts, intervals, counts and concurrency must be > 0")
        seen.add(row["id"])
        rows.append({"target_id": row.pop("id")} | row)
    return rows
