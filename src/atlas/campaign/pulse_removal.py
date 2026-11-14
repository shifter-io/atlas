"""PULSE-COMPACT-1: removing an hour's pulse droplets, REMOVE_CONCURRENCY at once.

Each removal is pulse_fleet's guarded path for one recorded node (pulse_fleet.retire_recorded:
its recorded id, the pulse tag, its name and project Dev Resources; then read until gone), about
12.5 s in production. One after the other, an hour's 144 droplets took about 30 minutes, so the
leftovers blocked the next hour's cap. `retire_many` runs REMOVE_CONCURRENCY of them at once
under one hold of the hour ledger's lock, with the same checks on every id and the same outcome
per node as pulse_fleet.retire (`destroyed`, `destroy_refused` or `destroy_failed`, saved once
at the end, also on a signal): a full hour goes in about ceil(144 / 16) x 12.5 s, under 2
minutes. A record left unsaved by a kill is removed again next time: its fresh GET is absent.

`remove_all` removes every recorded node of an hour not yet gone (an in-flight create's droplet
taken first by its exact name); `remove_leftovers` removes the leftovers of finished hours
found by the next hour's cap check through the hour ledgers that record them.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..onboarding.files import lock
from . import pulse_fleet
from .contracts import PULSE_PROJECT_NAME, PULSE_TAG

REMOVE_CONCURRENCY = 16  # guarded removals at once (each a few API calls, about 12.5 s)
LEDGER_HOURS = 48  # the newest hour ledgers searched for a leftover's record


async def retire_many(root: Path, names, cloud, clock) -> dict[str, str | None]:
    """Remove the recorded nodes `names` of a ledger through the guarded path, at most
    REMOVE_CONCURRENCY at once; returns each name's reason it is not verified gone (None: gone).
    A refusal or an error of one never stops the others; the lock held elsewhere, an unreadable
    ledger or a failed project lookup is each node's reason, so the caller still alerts."""
    names = sorted(set(names))
    if not names:
        return {}
    try:
        with lock(root / ".lock"):
            return await _retire_locked(root, names, cloud, clock)
    except Exception as error:  # boundary: reported per node, the caller goes on
        return dict.fromkeys(names, type(error).__name__)


async def _retire_locked(root: Path, names: list[str], cloud, clock) -> dict[str, str | None]:
    """retire_many under the hour ledger's lock."""
    records = pulse_fleet.load_nodes(root)
    project_id = await cloud.project_id(PULSE_PROJECT_NAME)
    gate = asyncio.Semaphore(REMOVE_CONCURRENCY)

    async def one(name: str) -> tuple[str, str | None]:
        async with gate:
            args = (root, records, name, project_id, cloud, clock)
            return name, await pulse_fleet.retire_recorded(*args)

    try:
        return dict(await asyncio.gather(*(one(name) for name in names)))
    finally:
        pulse_fleet.save_nodes(root, records)


async def adopt_in_flight(hour_root: Path, cloud) -> list[dict]:
    """Records a stopped create left `requested` without an id: take each one's droplet by its
    exact name under the pulse tag. A name with none or several stays unresolved, left over."""
    records = pulse_fleet.load_nodes(hour_root)
    waiting = sorted(n for n, r in records.items() if r.status == "requested" and not r.droplet_id)
    if not waiting:
        return []
    found = await cloud.adopt_uncertain(waiting, PULSE_TAG)
    left = []
    for name in waiting:
        matches = [d.id for d in found if d.name == name]
        if len(matches) == 1:
            records[name].droplet_id = matches[0]
        else:
            left.append({"name": name, "id": None, "reason": f"in-flight create: {len(matches)}"})
    pulse_fleet.save_nodes(hour_root, records)
    return left


async def remove_all(hour_root: Path, cloud, clock) -> list[dict]:
    """Retire every recorded node not yet gone, an in-flight create's taken first by name;
    returns those not verified gone."""
    left = await adopt_in_flight(hour_root, cloud)
    records = pulse_fleet.load_nodes(hour_root)
    up = [n for n, r in records.items() if r.droplet_id and r.status != "destroyed"]
    reasons = await retire_many(hour_root, up, cloud, clock)  # in parallel, guarded
    left += [
        {"name": name, "id": records[name].droplet_id, "reason": reason}
        for name, reason in sorted(reasons.items())
        if reason
    ]
    return left


def _ledgers(root: Path) -> dict[str, tuple[Path, str]]:
    """Each droplet id the newest hour ledgers record: its hour root and node name."""
    found = {}
    for hour_root in sorted((root / "hours").glob("*T*"))[-LEDGER_HOURS:]:
        try:
            records = pulse_fleet.load_nodes(hour_root)
        except (OSError, ValueError, TypeError, KeyError):  # corrupt: never blocks the hour
            continue
        for name, record in records.items():
            if record.droplet_id:
                found[record.droplet_id] = (hour_root, name)
    return found


async def remove_leftovers(root: Path, old: list, cloud, clock) -> list[dict]:
    """Remove the leftover droplets `old` through the hour ledgers of the pulse root that record
    them (the guarded path, in parallel); one entry per droplet: removed, or why not."""
    ledgers = _ledgers(root)
    by_root: dict[Path, dict[str, str]] = {}
    for droplet in old:
        if droplet.id in ledgers:
            hour_root, name = ledgers[droplet.id]
            by_root.setdefault(hour_root, {})[name] = droplet.id
    reasons = {}
    for hour_root, names in sorted(by_root.items()):
        for name, reason in (await retire_many(hour_root, names, cloud, clock)).items():
            reasons[names[name]] = reason
    return [
        {"name": d.name, "id": d.id, "created_at": d.created_at.isoformat()}
        | {"removed": d.id in reasons and not reasons[d.id]}
        | {"reason": reasons[d.id] if d.id in reasons else "in no hour ledger"}
        for d in sorted(old, key=lambda d: d.name)
    ]
