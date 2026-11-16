"""MANAGER-ORCH-1: the daily test on the manager, one hour at a time (`daily hour`, `daily check`).

The manager holds its own inputs: the formulas (`formulas/<network>.json`, each country's test
hour per network) and the template campaign, at fixed paths installed and checked by
`campaign manager orchestrator-enable`. `daily hour` runs the next hour's test (`daily run`) and
then imports it on the manager itself (`ingest_campaign` on the local hour root, no download),
spooling each run's raw files to the orchestrator's `raw.spool_dir` setting. Its systemd timer
is installed off and runs only after `campaign manager hourly-enable` (the owner's go).

A network with a connection budget may run past the others (daily.overtime): the import of the
hour does not wait for it. As soon as every other network's cells have ended (runner `ready`),
they are imported in a thread of their own, without the hour's audits, while the budgeted cells
still run; once the run ends, the budgeted network's cells are imported and the hour's audits
written over the runs of both parts (ingest.Part, DAILY-BUDGET-STOP-1). An early part that
fails is imported again with the rest.

`daily check` proves the formulas and the template fit for all 24 hours: every formula loads,
every template manifest is an ephemeral campaign, no hour has a due network the template
cannot test or a due region it does not cover, and a budgeted network's droplets of every hour fit
its connection budget (daily.budget; `budget_by_hour` lists their concurrency).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .. import timing
from ..campaign.tick import load_spec
from ..onboarding import manifest
from .ingest import Part, ingest_part
from .planner import load_schedules, plan_hour
from .runner import RunError, RunInputs, hour_budget, run_hour

log = logging.getLogger(__name__)


def check(formulas: Path, template: Path) -> dict:
    """The 24-hour fit of the formulas and the template; `ok` is False with `issues`."""
    issues: list[str] = []
    try:
        schedules = load_schedules(formulas)
        base = load_spec(template)
    except (OSError, ValueError) as exc:
        return {"ok": False, "issues": [f"{type(exc).__name__}: {exc}"]}
    if not schedules:
        issues.append("no formula")
    for network in base.networks:
        if manifest.load(template / base.manifests[network]).get("execution_mode") != "ephemeral":
            issues.append(f"{network}: the template manifest is not an ephemeral campaign")
    droplets, shares = {}, {}
    for hour in range(24):
        try:
            plan = plan_hour(schedules, hour, networks=base.networks)
        except ValueError as exc:
            issues.append(f"hour {hour}: {exc}")
            continue
        missing = [region for region in plan.regions() if region not in base.regions]
        if missing:
            issues.append(f"hour {hour}: the template has no region {', '.join(missing)}")
        if plan.skipped:
            issues.append(f"hour {hour}: the template cannot test {', '.join(plan.skipped)}")
        droplets[str(hour)] = len(plan.cells)
        try:
            found = hour_budget(template, base, plan)
        except RunError as exc:
            issues.append(f"hour {hour}: {exc}")
            continue
        if found:
            shares[str(hour)] = {network: entry["concurrency"] for network, entry in found.items()}
    return {
        "ok": not issues,
        "issues": issues,
        "networks": sorted(schedules),
        "countries": sum(len(schedule) for schedule in schedules.values()),
        "droplets_by_hour": droplets,
    } | ({"budget_by_hour": shares} if shares else {})


async def spool_dir(database_file: Path) -> Path | None:
    """The orchestrator's `raw.spool_dir` setting (None keeps no raw files)."""
    from ..orchestrator.db import Database, setting

    db = Database(str(database_file), size=1)
    try:
        async with db.connection() as conn:
            value = await setting(conn, "raw.spool_dir")
    finally:
        await db.close()
    return Path(value) if value else None


async def _import(root: Path, database_file: Path, mmdb, formulas: Path, part: Part) -> dict:
    """One part of the hour's import (ingest.ingest_part), with the orchestrator's spool."""
    spool = await spool_dir(database_file)
    return await ingest_part(root, database_file, mmdb, part, formulas=formulas, spool=spool)


async def _early(early: dict) -> dict | None:
    """The early part's report, None when it failed (its networks go with the rest)."""
    try:
        return await early["task"]
    except Exception as error:  # boundary: the rest's import takes the early part's networks
        log.warning("daily hour: early import failed (%s)", type(error).__name__)
        return None


async def run_and_ingest(
    formulas: Path, template: Path, database_file: Path, mmdb: Path | None, start: str
) -> dict:
    """Run the hour's test, then import whatever it measured (also after a failed hour); the
    networks that end before a budgeted one are imported as soon as they end.
    `template` is the resolved template root (the hour roots go beside it, not its link)."""
    early = {}

    def ready(root: Path, networks: frozenset) -> None:
        part = Part(networks, audits=False)
        work = asyncio.run, _import(root, database_file, mmdb, formulas, part)
        early.update(networks=networks, task=asyncio.ensure_future(asyncio.to_thread(*work)))

    with timing.stage("hour", "run"):
        outcome = await run_hour(RunInputs(template, formulas, start, database_file), ready=ready)
    if outcome["state"] == "nothing_due":
        return outcome | {"ingest": None}
    root = template.parent / outcome["campaign_id"]
    first = await _early(early) if early else None
    rest = Part()
    if first is not None:
        networks = frozenset(load_spec(root).networks) - early["networks"]
        rest = Part(networks, earlier=first.get("tested", {}))
    report = await _import(root, database_file, mmdb, formulas, rest)
    parts = [report, *([first] if first else [])]
    cells = sum(len(part["cells"]) for part in parts)
    return outcome | {"ingest": {"complete": all(p["complete"] for p in parts), "cells": cells}}
