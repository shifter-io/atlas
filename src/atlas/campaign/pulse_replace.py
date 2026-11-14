"""PULSE-REPLACE-1: arming an hour's pulse droplets, with one replacement for a droplet that never
boots.

A droplet ends `boot_failed` when it never became active or never answered SSH within the pulse
fleet's limits. Such a node is replaced once, at once: its droplet is removed through the
guarded path (pulse_removal.retire_many) and the pulse fleet creates and arms one new droplet for
the same cell (`Fleet.reconcile` for that cell only: the same create, project and arming
checks; one for one, so the hour's cap still holds). The ledger record of the cell then counts
two `attempts`; the hour's `replaced` lists the old droplet, the reason and whether the new one
armed. A replacement that fails too, or an old droplet not verified gone, leaves the cell lost.

A pulse batch is one per country, target and hour, shared by every network of the country's
region, and a run must start within batch.start_grace_seconds of its batch, so a replacement
armed minutes later would find its region's batches already run. The hour therefore opens in
two steps (campaign.pulse_ondemand): first the regions where no droplet failed to boot, then,
after the replacements, the regions that had one (their other droplets wait those 2 to 3
minutes). `opened` merges the two open_hour results.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ..onboarding.files import lock
from . import pulse_fleet
from .pulse_fleet import Fleet, FleetClients, FleetConfig, node_name
from .pulse_removal import retire_many

REPLACEABLE = frozenset({"boot_failed"})  # never active, or SSH never ready


async def arm(hour_root: Path, config: FleetConfig, s) -> dict:
    """One pulse fleet pass for `config`'s cells under the hour ledger's lock; its report."""
    with lock(hour_root / ".lock"):
        fleet = Fleet(hour_root, config, FleetClients(s.cloud, s.remote, s.issuer, s.clock))
        return await fleet.reconcile()


def boot_failed(config: FleetConfig, records: dict) -> list[str]:
    """The names of the cells' nodes that never booted."""
    names = [node_name(network, region) for network, region in config.cells or ()]
    return [name for name in names if name in records and records[name].status in REPLACEABLE]


async def replace_once(hour_root: Path, config: FleetConfig, names: list[str], s) -> list[dict]:
    """Remove each named node's droplet (guarded) and arm one new droplet for its cell; one
    entry per name: the old droplet, the reason, whether it was removed and the new one armed.
    A failed replacement costs only its own cells (lost), never the hour."""
    old = pulse_fleet.load_nodes(hour_root)
    reasons = await retire_many(hour_root, names, s.cloud, s.clock)
    gone = {name for name in names if reasons.get(name) is None}
    cells = tuple(cell for cell in config.cells or () if node_name(*cell) in gone)
    error = None
    if cells:
        regions = tuple(sorted({region for _, region in cells}))
        try:
            await arm(hour_root, replace(config, cells=cells, regions=regions), s)
        except Exception as caught:  # boundary: the other regions' pulses go on
            error = type(caught).__name__
    records = pulse_fleet.load_nodes(hour_root)
    return [
        {"name": name, "old_id": old[name].droplet_id, "reason": old[name].status}
        | {"removed": name in gone, "armed": name in gone and records[name].status == "armed"}
        | ({"error": error} if error and name in gone else {})
        for name in sorted(names)
    ]


def merge_opened(results: list[dict]) -> dict:
    """The open_hour results of the hour's steps as one; {"created": 0} when none opened."""
    if not results:
        return {"created": 0}
    done = [r for r in results if "skipped" not in r]
    if not done:
        return results[0]
    return {
        "targets": sorted({t for r in done for t in r["targets"]}),
        "created": sum(r["created"] for r in done),
        "runs": sum(r["runs"] for r in done),
        "not_hourly": done[0]["not_hourly"],
    }
