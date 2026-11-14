"""PULSE-COMPACT-1: removing an hour's pulse droplets, REMOVE_CONCURRENCY at once.

Each removal is pulse_fleet's guarded path for one recorded node (pulse_fleet.retire_recorded:
its recorded id, the pulse tag, its name and project Dev Resources; then read until gone), about
12.5 s in production. One after the other, an hour's 144 droplets took about 30 minutes, so the
leftovers blocked the next hour's cap. `retire_many` runs REMOVE_CONCURRENCY of them at once
under one hold of the hour ledger's lock, with the same checks on every id and the same outcome
per node as pulse_fleet.retire (`destroyed`, `destroy_refused` or `destroy_failed`, saved once
at the end, also on a signal): a full hour goes in about ceil(144 / 16) x 12.5 s, under 2
minutes. A record left unsaved by a kill is removed again next time: its fresh GET is absent.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..onboarding.files import lock
from . import pulse_fleet
from .contracts import PULSE_PROJECT_NAME

REMOVE_CONCURRENCY = 16  # guarded removals at once (each a few API calls, about 12.5 s)


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
