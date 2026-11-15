"""MANAGER-ORCH-1: the daily test on the manager, one hour at a time (`daily hour`, `daily check`).

The manager holds its own inputs: the formulas (`formulas/<network>.json`, each country's test
hour per network) and the template campaign, at fixed paths installed and checked by
`campaign manager orchestrator-enable`. `daily hour` runs the next hour's test (`daily run`) and
then imports it on the manager itself (`ingest_campaign` on the local hour root, no download),
spooling each run's raw files to the orchestrator's `raw.spool_dir` setting. Its systemd timer
is installed off and runs only after `campaign manager hourly-enable` (the owner's go).

`daily check` proves the formulas and the template fit for all 24 hours: every formula loads,
every template manifest is an ephemeral campaign, no hour has a due network the template
cannot test or a due region it does not cover, and a budgeted network's droplets of every hour fit
its connection budget (daily.budget; `budget_by_hour` lists their concurrency).
"""

from __future__ import annotations

from pathlib import Path

from .. import timing
from ..campaign.tick import load_spec
from ..onboarding import manifest
from .ingest import ingest_campaign
from .planner import load_schedules, plan_hour
from .runner import RunError, RunInputs, hour_budget, run_hour


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


async def run_and_ingest(
    formulas: Path, template: Path, database_file: Path, mmdb: Path | None, start: str
) -> dict:
    """Run the hour's test, then import whatever it measured (also after a failed hour).
    `template` is the resolved template root (the hour roots go beside it, not its link)."""
    with timing.stage("hour", "run"):
        outcome = await run_hour(RunInputs(template, formulas, start, database_file))
    if outcome["state"] == "nothing_due":
        return outcome | {"ingest": None}
    report = await ingest_campaign(
        template.parent / outcome["campaign_id"],
        database_file,
        mmdb,
        formulas=formulas,
        spool=await spool_dir(database_file),
    )
    return outcome | {"ingest": {"complete": report["complete"], "cells": len(report["cells"])}}
