"""`daily run`: one hour's daily test, through the 24-hour campaign's own machinery.

Runs on the manager like `campaign tick`. A planned template campaign (`campaign plan`, uploaded
with `manager upload-spec`) supplies the account, project, images, worker key, wheel and each
network's manifest. For the hour, the runner writes a fresh one-slot campaign beside it: a new
campaign id, and per network a manifest re-created for this hour with the template's settings
(same concurrency and stop rule as the 24-hour rounds, same target and HTTPS redirect) and the
countries due (`due_countries`). `tick.run_tick` launches slot 0 (one worker per network and
region with something due), `collector.run_collector` harvests it, and whatever happens every
recorded worker still up is removed through `scope.guarded_delete` and checked gone: after a
failure, a signal (`daily run` cancels on SIGTERM, SIGHUP and SIGINT; the removal is shielded) or
the run's time limit (the hour's slot plus `RUN_MARGIN_MINUTES`). A worker whose create was in
flight is first taken by its exact name under the hour tag. The run's outcome is written to
`<root>/daily.json` (names and counts, never an address).
"""

from __future__ import annotations

import asyncio
import secrets
import shutil
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..campaign import collector
from ..campaign.collector import Pacing, run_collector
from ..campaign.contracts import CampaignSpec, hour_path, hour_tag, spec_path
from ..campaign.launch import HourWork, take_in_flight
from ..campaign.tick import load_hour, load_spec, run_tick, utc_now
from ..onboarding import manifest
from ..onboarding.files import atomic_bytes, atomic_json, lock
from ..onboarding.scheduler import Clock
from .planner import HourPlan, load_schedules, plan_hour

GONE_CHECKS = 6  # reads of a removed worker before it counts as left over
GONE_POLL_SECONDS = 5.0
RUN_MARGIN_MINUTES = 30  # after the slot's end the run stops waiting and cleans up


class RunError(ValueError):
    """An input the hour's test cannot run with; the message names it, never a secret."""


@dataclass(frozen=True)
class RunInputs:
    template: Path  # a planned campaign root on the manager
    formulas: Path  # the formula JSONs (`formulas/<network>.json`)
    start: str = "now-hour"  # "now-hour", "next-hour" or an ISO whole hour with offset


def start_hour(value: str, now: datetime) -> tuple[datetime, bool]:
    """(the test's whole UTC hour, whether it is the current one)."""
    current = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    if value == "now-hour":
        return current, True
    if value == "next-hour":
        return current + timedelta(hours=1), False
    begins = datetime.fromisoformat(value)
    if begins.utcoffset() is None:
        raise RunError("--start needs an explicit UTC offset")
    begins = begins.astimezone(UTC)
    return begins, begins == current


def time_limit(begins: datetime, now: datetime, slot_minutes: int) -> float:
    """Seconds the run may take: until the slot's end plus the margin."""
    end = begins + timedelta(minutes=slot_minutes + RUN_MARGIN_MINUTES)
    return max(0.0, (end - now).total_seconds())


def hour_plan(inputs: RunInputs, now: datetime) -> tuple[CampaignSpec, HourPlan, datetime, bool]:
    """The template's spec, the hour's cells and the hour; nothing is written."""
    begins, current = start_hour(inputs.start, now)
    base = load_spec(inputs.template)
    plan = plan_hour(load_schedules(inputs.formulas), begins.hour, networks=base.networks)
    missing = [r for r in plan.regions() if r not in base.regions]
    if missing:
        raise RunError(f"the template campaign has no region {', '.join(missing)}")
    return base, plan, begins, current


def _manifest(
    template: Path, base: CampaignSpec, root: Path, network: str, plan: HourPlan, hour: tuple
) -> str:
    """The network's manifest for this hour: the template's settings, the countries due."""
    begins, current, now = hour
    source = template / base.manifests[network]
    old = manifest.load(source)
    if old.get("execution_mode") != "ephemeral":
        raise RunError(f"{network}: the template manifest is not an ephemeral campaign")
    config = source.parent / "provider.toml"
    run_settings = dict(old["settings"])
    retry = bool(run_settings.pop("retry_http_469", False))
    classes = bool(run_settings.pop("answer_classes", False))
    path = manifest.create(
        config,
        old["product"],
        begins.isoformat(),
        root / "networks",
        now=now,
        execution_mode="ephemeral",
        grace_hours=old["grace_hours"],
        allow_current_hour=current,
        run_settings=run_settings,
        retry_http_469=retry,
        answer_classes=classes,
        target_url=old["target_url"],
        slot_minutes=manifest.slot_minutes(old),
        due_countries=plan.due(network),
    )
    if manifest.load(path)["settings"] != old["settings"]:
        raise RunError(f"{network}: the hour's settings differ from the template's")
    atomic_bytes(path.parent / "provider.toml", config.read_bytes())
    return path.relative_to(root).as_posix()


def prepare(
    template: Path, base: CampaignSpec, plan: HourPlan, begins: datetime, current: bool, now
) -> Path:
    """Write the hour's one-slot campaign beside the template; returns its root."""
    cid = f"{begins:%Y%m%dt%H%M%S}z-{secrets.token_hex(6)}"
    root = template.parent / cid
    root.mkdir(mode=0o700)
    try:
        regions = plan.regions()
        manifests = {
            n: _manifest(template, base, root, n, plan, (begins, current, now))
            for n in plan.networks()
        }
        spec = replace(
            base,
            campaign_id=cid,
            created_at=now.isoformat(),
            starts_at=begins.isoformat(),
            regions=regions,
            networks=plan.networks(),
            manifests=manifests,
            size_by_region={r: base.size_by_region[r] for r in regions},
            image_by_region={r: base.image_by_region[r] for r in regions},
            hours=1,
        ).validate()
        atomic_bytes(spec_path(root), spec.to_json().encode())
    except Exception:
        shutil.rmtree(root, ignore_errors=True)  # only the folder this run just made
        raise
    return root


async def clean_up(
    root: Path, spec: CampaignSpec, cloud, clock=None
) -> tuple[list[str], str | None]:
    """Remove every recorded worker not yet destroyed (guarded), a worker whose create was in
    flight taken first by its exact name under the hour tag. Returns the ids of the hour's
    workers (recorded or still tagged) not verified gone, and why none was removed, if so."""
    if spec.synthetic or cloud is None:
        return [], None
    clock = clock or Clock()
    record = load_hour(root, spec, 0)
    skipped = None
    try:
        with lock(hour_path(root, 0).parent / ".lock"):
            await take_in_flight(spec, record, root, cloud, clock)
            work = HourWork(spec, record, root, cloud, clock)
            for worker in record.droplets:
                if worker.droplet_id and worker.status != "destroyed":
                    await work.retire(worker)
            work.save()
    except ValueError:  # the hour is locked by a launcher still running: only check
        skipped = "not retired: the hour is locked by a launcher still running"
    ids = {str(d.droplet_id) for d in record.droplets if d.droplet_id}
    ids |= {d.id for d in await cloud.list_tag(hour_tag(spec.campaign_id, 0))}
    left = sorted(ids)
    for attempt in range(GONE_CHECKS):
        left = [i for i in left if await cloud.get(i) is not None]
        if not left or attempt == GONE_CHECKS - 1:
            break
        await clock.sleep(GONE_POLL_SECONDS)
    return left, skipped


async def run_hour(
    inputs: RunInputs,
    *,
    services: Callable = collector.services,
    now: datetime | None = None,
    pacing: Pacing | None = None,
    clock=None,
    limit: float | None = None,
) -> dict:
    """Run the hour's daily test; returns the outcome written to `<root>/daily.json`.

    `limit` (seconds) defaults to `time_limit`; a cancellation (a signal) or the limit ends the
    run as failed, and the clean-up then runs to its end even if cancelled again."""
    now = now or utc_now()
    base, plan, begins, current = hour_plan(inputs, now)
    if not plan.cells:
        return {"state": "nothing_due", "plan": plan.to_dict()}
    root = prepare(inputs.template, base, plan, begins, current, now)
    spec = load_spec(root)
    limit = time_limit(begins, now, spec.slot_minutes) if limit is None else limit
    cloud, remote_factory, launcher, harvester = services(root, spec)
    outcome = {
        "campaign_id": spec.campaign_id,
        "plan": plan.to_dict(),
        "state": "failed",
        "leftover": None,  # unknown until the clean-up has checked
    }
    error = None

    async def work() -> None:
        await run_tick(root, launcher=launcher, now_mode=True, slot=0)
        await run_collector(
            root,
            harvester=harvester,
            cloud=cloud,
            remote_factory=remote_factory,
            until_done=True,
            pacing=pacing,
        )

    try:
        await asyncio.wait_for(work(), limit)
        outcome["state"] = load_hour(root, spec, 0).state
    except TimeoutError:
        outcome["error"] = f"stopped at the time limit ({limit:.0f} s)"
    except asyncio.CancelledError:
        outcome["error"] = "cancelled by a signal"
    except BaseException as caught:  # an interrupt too: the workers still go
        error = caught
        outcome["error"] = type(caught).__name__
    cleanup = asyncio.ensure_future(clean_up(root, spec, cloud, clock))
    try:
        while not cleanup.done():  # a signal during the removal never interrupts it
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                outcome["error"] = "cancelled by a signal"
        outcome["leftover"], skipped = cleanup.result()
        if skipped:
            outcome["cleanup"] = skipped
    finally:
        if cloud is not None:
            await cloud.close()
        atomic_json(root / "daily.json", outcome)
    if error is not None:
        raise error
    return outcome
