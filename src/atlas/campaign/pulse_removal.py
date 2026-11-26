"""PULSE-COMPACT-1: removing an hour's pulse droplets, REMOVE_CONCURRENCY at once.

Each removal is pulse_fleet's guarded path for one recorded node (pulse_fleet.retire_recorded:
its recorded id, the pulse tag, its name and project Dev Resources; then read until gone), about
12.5 s in production. One after the other, an hour's 144 droplets took about 30 minutes, so the
leftovers blocked the next hour's cap. `retire_many` runs REMOVE_CONCURRENCY of them at once
under one hold of the hour ledger's lock, with the same checks on every id and the same outcome
per node as pulse_fleet.retire (`destroyed`, `destroy_refused` or `destroy_failed`, saved once
at the end, also on a signal): a full hour goes in about ceil(144 / 16) x 12.5 s, under 2
minutes. A record left unsaved by a kill is removed again next time: its fresh GET is absent.

`remove_all` removes every recorded node of an hour not yet gone; `remove_leftovers` removes the
leftovers of finished hours found by the next hour's cap check through the hour ledgers that
record them. Both first adopt the droplets of the ledgers' unsure creates (a lost answer, or a
create a stopped process left `requested`; pulse_adopt: its own create's proof, never a name
alone) and remove them on the guard's own-unassigned path (pulse_fleet.remove_unassigned): they
never reached Dev Resources (PULSE-TEARDOWN-1).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ..onboarding.files import lock
from . import pulse_adopt, pulse_fleet
from .contracts import PULSE_PROJECT_NAME

REMOVE_CONCURRENCY = 16  # guarded removals at once (each a few API calls, about 12.5 s)
LEDGER_HOURS = 48  # the newest hour ledgers searched for a leftover's record
# A record in one of these, or with the guard's ledger mark (its assignment failed), ended its
# droplet's work: a young droplet it holds is a leftover too.
GIVEN_UP = frozenset({"destroy_refused", "destroy_failed", "boot_failed", "start_failed"})
LOG = logging.getLogger(__name__)


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


async def adopt_unsure(hour_root: Path, cloud, exclude=frozenset()) -> list[dict]:
    """The ledger's unsure creates (pulse_adopt.unsure): adopt each one's droplet (none of the
    ids in `exclude` or the ledger) and remove it on the own-unassigned path, under the hour
    ledger's lock. A `requested` one without a droplet stays unresolved, left over; a lost
    answer's without one made none."""
    with lock(hour_root / ".lock"):
        records = pulse_fleet.load_nodes(hour_root)
        unsure = {n: r for n, r in records.items() if pulse_adopt.unsure(r)}
        if not unsure:
            return []
        recorded = {r.droplet_id for r in records.values() if r.droplet_id}
        found = await pulse_adopt.adopt(cloud, unsure, set(exclude) | recorded)
        for name, droplet in found.items():
            records[name].droplet_id = droplet.id
        pulse_fleet.save_nodes(hour_root, records)
        if found:
            LOG.warning("pulse droplets of unsure creates adopted: %s", ", ".join(sorted(found)))
            ids = {droplet.id: name for name, droplet in found.items()}
            await pulse_fleet.remove_unassigned(hour_root, records, ids, cloud)
    return [
        {"name": name, "id": None, "reason": "in-flight create: not found"}
        for name in sorted(unsure)
        if name not in found and records[name].status == "requested"
    ]


async def remove_all(hour_root: Path, cloud, clock) -> list[dict]:
    """Retire every recorded node not yet gone, an unsure create's adopted and removed first;
    returns those not verified gone."""
    try:
        left = await adopt_unsure(hour_root, cloud)
    except Exception as error:  # boundary: the recorded nodes still go; listed, so alerted
        left = [{"name": "unsure creates", "id": None, "reason": type(error).__name__}]
    records = pulse_fleet.load_nodes(hour_root)
    up = [n for n, r in records.items() if r.droplet_id and r.status != "destroyed"]
    reasons = await retire_many(hour_root, up, cloud, clock)  # in parallel, guarded
    left += [
        {"name": name, "id": records[name].droplet_id, "reason": reason}
        for name, reason in sorted(reasons.items())
        if reason
    ]
    return left


def _hour_roots(root: Path) -> list[Path]:
    return sorted((root / "hours").glob("*T*"))[-LEDGER_HOURS:]


def _ledgers(root: Path) -> dict[str, tuple[Path, str, bool]]:
    """Each droplet id the newest hour ledgers record: its hour root, node name and whether its
    record gave it up (GIVEN_UP, or the ledger mark)."""
    found = {}
    for hour_root in _hour_roots(root):
        try:
            records = pulse_fleet.load_nodes(hour_root)
        except (OSError, ValueError, TypeError, KeyError):  # corrupt: never blocks the hour
            continue
        for name, record in records.items():
            if record.droplet_id:
                dead = record.status in GIVEN_UP or bool(record.assign_failed_at)
                found[record.droplet_id] = (hour_root, name, dead)
    return found


def finished(root: Path, hour: str) -> set[str]:
    """The droplet ids no running hour can still use (PULSE-TEARDOWN-1): recorded by the ledger
    of an hour before `hour` (YYYYMMDDTHH; its job ended by STORE_BY, or a reset killed it before
    its removal), or given up by their record in any ledger."""
    return {
        rid for rid, (hour_root, _, dead) in _ledgers(root).items() if dead or hour_root.name < hour
    }


async def _adopt_ledgers(root: Path, cloud, exclude: set[str]) -> None:
    """adopt_unsure in each of the newest hour ledgers; a held lock or an error skips one."""
    for hour_root in _hour_roots(root):
        try:
            await adopt_unsure(hour_root, cloud, exclude)
        except Exception as error:  # boundary: never blocks the hour; listed if still up
            LOG.warning("pulse ledger %s not adopted: %s", hour_root.name, type(error).__name__)


async def remove_leftovers(root: Path, old: list, cloud, clock) -> list[dict]:
    """Remove the leftover droplets `old` through the hour ledgers of the pulse root that record
    them (the guarded path, in parallel); one entry per droplet: removed, or why not. A
    droplet no ledger records may be an unsure create's: the ledgers adopt theirs first."""
    ledgers = _ledgers(root)
    if any(droplet.id not in ledgers for droplet in old):
        await _adopt_ledgers(root, cloud, set(ledgers))
        ledgers = _ledgers(root)
    by_root: dict[Path, dict[str, str]] = {}
    for droplet in old:
        if droplet.id in ledgers:
            hour_root, name, _ = ledgers[droplet.id]
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
