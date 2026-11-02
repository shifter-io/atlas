"""Which (network, country) pairs are due in a UTC hour, grouped into droplet cells.

Each network's formula (`formulas/<network>.json`, FORMULA-1) gives every country one test hour
(`schedule`: {"DE": 21, ...}). In hour H the planner takes every pair whose hour is H, groups
the countries of each network by the runner region that serves them (configs/regions.yaml) and
returns one cell per network and region with something due: one droplet each. A region with
nothing due for a network gets no droplet. Countries keep the region's manifest order.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..regions import REGION_IDS, RegionMap, load_regions


@dataclass(frozen=True, slots=True)
class Cell:
    """One droplet: a network in a region, with the countries due there this hour."""

    network: str
    region: str
    countries: tuple[str, ...]

    @property
    def region_id(self) -> int:
        return REGION_IDS[self.region]


@dataclass(frozen=True, slots=True)
class HourPlan:
    hour: int
    cells: tuple[Cell, ...]
    skipped: tuple[str, ...] = ()  # due networks the runner cannot test (no template entry)

    def networks(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(cell.network for cell in self.cells))

    def regions(self) -> tuple[str, ...]:
        return tuple(r for r in REGION_IDS if any(c.region == r for c in self.cells))

    def due(self, network: str) -> dict[str, list[str]]:
        """{region: [countries]} of one network, the manifest's `due_countries`."""
        return {c.region: list(c.countries) for c in self.cells if c.network == network}

    def to_dict(self) -> dict:
        return {
            "hour": self.hour,
            "droplets": len(self.cells),
            "networks": list(self.networks()),
            "regions": list(self.regions()),
            "cells": [
                {"network": c.network, "region": c.region, "countries": list(c.countries)}
                for c in self.cells
            ],
            "skipped": list(self.skipped),
        }


def load_schedules(directory: Path) -> dict[str, dict[str, int]]:
    """{network: {country: hour}} from every formula JSON in `directory`."""
    schedules = {}
    for path in sorted(Path(directory).glob("*.json")):
        formula = json.loads(path.read_text())
        network, schedule = formula.get("network"), formula.get("schedule")
        if not isinstance(network, str) or not isinstance(schedule, dict) or not schedule:
            raise ValueError(f"{path.name}: a formula needs a network and a schedule")
        if network in schedules:
            raise ValueError(f"{path.name}: network {network} has two formulas")
        for country, hour in schedule.items():
            if not (isinstance(country, str) and len(country) == 2 and country.isupper()):
                raise ValueError(f"{path.name}: invalid country code {country!r}")
            if type(hour) is not int or not 0 <= hour <= 23:
                raise ValueError(f"{path.name}: {country} needs a test hour 0..23")
        schedules[network] = dict(schedule)
    return schedules


def plan_hour(
    schedules: dict[str, dict[str, int]],
    hour: int,
    *,
    networks: tuple[str, ...] | None = None,
    regions: RegionMap | None = None,
) -> HourPlan:
    """The cells due in UTC `hour`; `networks` limits them to the ones the runner can test."""
    if type(hour) is not int or not 0 <= hour <= 23:
        raise ValueError("hour must be 0..23")
    mapping = regions or load_regions()
    cells, skipped = [], []
    for network in sorted(schedules):
        due = {code for code, at in schedules[network].items() if at == hour}
        if not due:
            continue
        if networks is not None and network not in networks:
            skipped.append(network)
            continue
        for region in mapping.regions:
            countries = tuple(code for code in region.countries if code in due)
            if countries:
                cells.append(Cell(network, region.slug, countries))
        unknown = due - {code for region in mapping.regions for code in region.countries}
        if unknown:
            raise ValueError(f"{network}: countries in no region: {', '.join(sorted(unknown))}")
    order = list(REGION_IDS)
    cells.sort(key=lambda c: (c.network, order.index(c.region)))
    return HourPlan(hour, tuple(cells), tuple(skipped))
