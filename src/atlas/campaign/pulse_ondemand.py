"""PULSE-ONDEMAND-1: one hour of on-demand pulses on the manager (`daily pulse-hour`).

The pulse root holds the pulse fleet's `fleet.json` (pulse_fleet.load_config: account, images,
key, wheel, orchestrator_url, database_file and each network's product, provider config and
credentials). For the hour the job plans one droplet per network and region (daily.pulses, the
networks of both the formulas and `fleet.json`), removes the leftovers and refuses the hour
over the cap, then, with its ledger in `<root>/hours/<YYYYMMDDTHH>/`:

1. creates and arms the hour's nodes through the pulse fleet (`Fleet.reconcile` with the hour's
   cells: create under the pulse tag, confirm project Dev Resources, push the node and start it)
   and replaces a node that never booted once (pulse_replace, PULSE-REPLACE-1: `replaced`);
2. opens the hour's pulses for the armed cells (orchestrator.pulse_hour.open_hour): the regions
   where every node booted at once, the others after their replacements;
3. waits until no run of the hour is left to store, at most until HARD_STOP after the job
   started less REMOVE_LEAD, retiring and removing at once each armed node whose runs of the
   hour are all stored (PULSE-COMPACT-1): a droplet lives about 15 to 20 minutes;
4. retires the nodes of every planned cell (orchestrator.pulse_hour.retire_nodes), so the
   manager's offline job raises no alert for them, then removes every recorded node left
   (pulse_removal.retire_many: the guarded path, 16 at once), whatever happened before: an
   error, the limit or a signal (both are shielded; PULSE-NODE-RETIRE-1).

The cap counts only the droplets under the pulse tag younger than LEFTOVER_AGE; an older one is
a leftover of a finished hour: never counted, removed through the hour ledger that records it,
and the leftovers share one major alert, `pulse_leftover`, one per hour (PULSE-COMPACT-1).

A node not verified gone opens the `pulse_teardown_failed` alert (major, one per hour) and is
listed in `leftover`. An hour that created none of its droplets (every create failed, or
`fleet.json` refused) opens `pulse_hour_failed` (major, one per hour; PULSE-FLEET-FIX-1). The
outcome is `<hour root>/pulse.json` (names and counts, no address): `created`, `create_failed`
(the count by error class) and each failed node's status, with the class and DigitalOcean's
message for a failed create. A planned cell with no armed node at the end is `lost`
("network/region"); the lost cells of an hour share one major alert, `pulse_cells_lost`.
`state` (_state): done, partial (cells lost or droplets left over), timed_out (a batch still had
a run queued or running at the deadline: `still_open`, where a run uploading to the manager
does not count), not_opened or failed.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..daily.planner import HourPlan, load_schedules
from ..daily.pulses import DEFAULT_CAP, check_cap, pulse_hour
from ..daily.runner import start_hour
from ..onboarding.files import atomic_json
from ..orchestrator.events import raise_alert
from ..orchestrator.pulse_hour import Armed, open_hour, retire_nodes
from . import current_wheel, pulse_fleet, pulse_replace
from .contracts import PULSE_TAG, with_legacy
from .current_wheel import CurrentWheel
from .pulse_fleet import FleetConfig, FleetError, node_name
from .pulse_removal import remove_all, remove_leftovers, retire_many

# A droplet's hard stop: created from HH:57, gone by HH+1:22 (the wait ends REMOVE_LEAD before).
HARD_STOP, REMOVE_LEAD = timedelta(minutes=25), timedelta(minutes=3)
LEFTOVER_AGE = timedelta(minutes=30)  # older at an hour's start: a finished hour's leftover
WAIT_POLL_SECONDS = 15.0
# The runs of the hour's open batches not stored yet, with their node (provider, product,
# region): `uploading` ones are with the manager already (PULSE-REPLACE-1).
BUSY_SQL = (
    "SELECT b.batch_id,br.status,p.provider,p.name AS product,r.slug AS region FROM batch_runs br "
    "JOIN test_batches b USING(batch_id) JOIN products p ON p.product_id=br.product_id "
    "JOIN runner_regions r ON r.region_id=b.region_id WHERE b.kind='pulse' "
    "AND b.scheduled_for=%s AND b.status IN ('scheduled','running') "
    "AND br.status NOT IN ('done','failed','skipped','killed')"
)


@dataclass(frozen=True)
class HourInputs:
    root: Path  # the pulse root: fleet.json, and hours/ for each hour's ledger
    formulas: Path
    start: str = "next-hour"
    cap: int = DEFAULT_CAP


@dataclass(frozen=True)
class Services:
    """The hour's cloud, SSH remote, node-token issuer, clock and orchestrator database."""

    cloud: object
    remote: object
    issuer: object
    clock: object
    db: object


def hour_config(
    config: FleetConfig, plan: HourPlan, code: CurrentWheel | None = None
) -> FleetConfig:
    """The fleet config of the hour: only its cells; every region needs an image. The
    manager's current wheel (IMAGE-AUTO-1), when there is one, replaces fleet.json's."""
    missing = [r for r in plan.regions() if r not in config.image_by_region]
    if missing:
        raise FleetError(f"fleet.json: no image for {', '.join(missing)}")
    cells = tuple((c.network, c.region) for c in plan.cells)
    wheel = code.wheel if code else config.wheel
    return replace(config, regions=plan.regions(), cells=cells, wheel=wheel)


def armed_cells(config: FleetConfig, plan: HourPlan, records: dict) -> dict[str, Armed]:
    """The cells whose node was armed, by node name, as open_hour takes them."""
    armed = {}
    for cell in plan.cells:
        name = node_name(cell.network, cell.region)
        record = records.get(name)
        if record is not None and record.status == "armed":
            network = config.networks[cell.network]
            armed[name] = Armed(network.provider, network.product, cell.region, cell.countries)
    return armed


async def remove_done(hour_root: Path, done: dict[str, Armed], s: Services) -> dict:
    """Retire the nodes whose work is stored and remove their droplets at once (the guarded
    path, in parallel); each one's reason it is not gone. The final removal retries a failure."""
    try:
        await retire_nodes(s.db, list(done.values()))
    except Exception:  # boundary: a failed retire never keeps the droplets up
        pass
    return await retire_many(hour_root, done, s.cloud, s.clock)


async def wait_and_remove(
    hour_root: Path, hour: datetime, armed: dict[str, Armed], s: Services, deadline: float
) -> tuple[int, list[str]]:
    """Poll until no run of the hour is left to store or the deadline; at every poll remove each
    armed node with none left, once (the final removal retries a failure). Returns how many
    batches still had a run queued or running (an upload to the manager does not count) and the
    nodes removed."""
    tried: dict[str, str | None] = {}
    while True:
        async with s.db.connection() as conn:
            rows = await (await conn.execute(BUSY_SQL, (hour,))).fetchall()
        busy = {(r["provider"], r["product"], r["region"]) for r in rows}
        open_ = len({r["batch_id"] for r in rows if r["status"] in ("queued", "running")})
        done = {
            name: cell
            for name, cell in armed.items()
            if name not in tried and (cell.provider, cell.product, cell.region) not in busy
        }
        if done:
            tried |= await remove_done(hour_root, done, s)
        if not rows or s.clock.now() + WAIT_POLL_SECONDS > deadline:
            return open_, [name for name, reason in tried.items() if reason is None]
        await s.clock.sleep(WAIT_POLL_SECONDS)


async def _alert(db, hour: datetime, kind: str, message: str, data: dict) -> None:
    """A major alert, one per kind and hour (Slack takes the alerts table's major rows)."""
    async with db.transaction() as conn:
        await raise_alert(
            conn,
            kind,
            message,
            severity="major",
            dedupe_key=f"{kind}:{hour:%Y%m%dT%H}",
            data={"hour": hour.isoformat()} | data,
        )


async def _no_droplets(db, hour: datetime, cause: str, data: dict) -> None:
    message = f"pulse hour {hour:%Y-%m-%d %H}:00 UTC: {cause}"
    await _alert(db, hour, "pulse_hour_failed", message, data)


def _failed(name: str, records: dict) -> dict:
    """A cell not armed: its status; for a failed create also its class and message."""
    record = records.get(name)
    if record is None:  # a name another droplet under the pulse tag still holds
        return {"name": name, "status": "not_created"}
    entry = {"name": name, "status": record.status}
    if record.status == "create_failed":
        message = (record.error_detail or {}).get("message")
        entry |= {"error_class": record.error_class, "message": message}
    return entry


async def _open(s: Services, hour: datetime, cells: list[Armed], results: list) -> None:
    if cells:
        now = datetime.fromtimestamp(s.clock.now(), UTC)
        results.append(await open_hour(s.db, hour, cells, now))


async def _arm_and_open(hour_root: Path, config, plan: HourPlan, hour: datetime, s: Services):
    """Arm the hour's nodes and open their pulses: the regions where every node booted at
    once, the others after their one replacement each (pulse_replace)."""
    report = await pulse_replace.arm(hour_root, config, s)
    records = pulse_fleet.load_nodes(hour_root)
    failing = pulse_replace.boot_failed(config, records)
    held = {records[name].region for name in failing}
    results: list[dict] = []
    first = armed_cells(config, plan, records).values()
    await _open(s, hour, [c for c in first if c.region not in held], results)
    replaced = await pulse_replace.replace_once(hour_root, config, failing, s) if failing else []
    records = pulse_fleet.load_nodes(hour_root)
    armed = armed_cells(config, plan, records)
    await _open(s, hour, [c for c in armed.values() if c.region in held], results)
    return report, records, armed, pulse_replace.merge_opened(results), replaced


async def _pulses(
    hour_root: Path, config, plan: HourPlan, hour: datetime, s: Services, deadline: float
) -> dict:
    """Create and arm the hour's nodes, open their pulses and remove each as it is done."""
    report, records, armed, opened, replaced = await _arm_and_open(
        hour_root, config, plan, hour, s
    )
    still_open, removed = 0, []
    if opened["created"] or opened.get("runs"):
        still_open, removed = await wait_and_remove(hour_root, hour, armed, s, deadline)
    names = [node_name(c.network, c.region) for c in plan.cells]
    armed_names = {n for n, r in records.items() if r.status == "armed"}
    failed = [_failed(n, records) for n in names if n not in armed_names]
    classes = Counter(f["error_class"] or "unknown" for f in failed if "error_class" in f)
    unarmed = {f["name"] for f in failed}
    cells = {node_name(c.network, c.region): f"{c.network}/{c.region}" for c in plan.cells}
    lost = [cell for name, cell in cells.items() if name in unarmed]
    return {
        "armed": len(armed),
        "replaced": replaced,
        "lost": lost,
        "created": sum(1 for n in names if n in records and records[n].droplet_id),
        "create_failed": dict(sorted(classes.items())),
        "failed": failed,
        "unknown": report["unknown"],
        "opened": opened,
        "still_open": still_open,
        "removed_when_done": len(removed),
    }


def planned_cells(config: FleetConfig, plan: HourPlan) -> list[Armed]:
    """Every planned cell as a node key, armed or not."""
    nets = config.networks
    return [
        Armed(nets[c.network].provider, nets[c.network].product, c.region, c.countries)
        for c in plan.cells
        if c.network in nets
    ]


async def retire_then_remove(hour_root: Path, cells: list[Armed], s: Services, outcome: dict):
    """Retire the hour's nodes, then remove their droplets: a failed retire never keeps a
    droplet up (its nodes then go offline, and the offline job retires them once the hour is
    closed)."""
    try:
        outcome["retired"] = await retire_nodes(s.db, cells)
    except Exception as error:  # boundary: the removal goes on
        outcome["retire_error"] = type(error).__name__
    return await remove_all(hour_root, s.cloud, s.clock)


async def _remove_shielded(
    hour_root: Path, hour: datetime, cells: list[Armed], s: Services, outcome: dict
) -> None:
    """Retire and remove the hour's nodes to the end, a signal never interrupting it; alert on
    leftovers."""
    removal = asyncio.ensure_future(retire_then_remove(hour_root, cells, s, outcome))
    while not removal.done():
        try:
            await asyncio.shield(removal)
        except asyncio.CancelledError:
            outcome["error"] = "cancelled by a signal"
    left = outcome["leftover"] = removal.result()
    if left:
        message = f"{len(left)} pulse droplets of {hour:%Y-%m-%d %H}:00 UTC are not verified gone"
        await _alert(s.db, hour, "pulse_teardown_failed", message, {"nodes": left})


def _state(outcome: dict) -> str:
    """The hour's final state (PULSE-REPLACE-1): not_opened, the orchestrator opened none;
    timed_out, a batch still had a run queued or running at the deadline; partial, cells lost
    (`lost`) or droplets not verified gone (`leftover`); else done."""
    if "skipped" in outcome["opened"]:
        return "not_opened"
    if outcome["still_open"]:
        return "timed_out"
    return "partial" if outcome["lost"] or outcome.get("leftover") else "done"


async def sweep_leftovers(root: Path, hour: datetime, s: Services, now: datetime) -> int:
    """The droplets under the pulse tag that may still be working (younger than LEFTOVER_AGE):
    the cap counts them. Older ones are leftovers: removed, with one grouped alert."""
    up = {d.id: d for tag in with_legacy([PULSE_TAG]) for d in await s.cloud.list_tag(tag)}
    old = [d for d in up.values() if now - d.created_at > LEFTOVER_AGE]
    if old:
        swept = await remove_leftovers(root, old, s.cloud, s.clock)
        gone = sum(1 for item in swept if item["removed"])
        message = (
            f"pulse hour {hour:%Y-%m-%d %H}:00 UTC: {len(old)} pulse droplets older than "
            f"{LEFTOVER_AGE.seconds // 60} minutes were still up; {gone} removed"
        )
        await _alert(s.db, hour, "pulse_leftover", message, {"nodes": swept})
    return len(up) - len(old)


async def _hour_plan(inputs: HourInputs, s: Services, hour: datetime, now: datetime):
    """The hour's fleet config, plan and wheel; refused over the cap or for a bad
    `fleet.json`."""
    config = pulse_fleet.load_config(inputs.root)
    schedules = load_schedules(inputs.formulas)
    plan = pulse_hour(schedules, hour.hour, networks=tuple(sorted(config.networks)))
    running = await sweep_leftovers(inputs.root, hour, s, now)
    check_cap(plan, inputs.cap, running=running)
    code = current_wheel.load(current_wheel.wheels_dir(inputs.root.parent))
    return hour_config(config, plan, code), plan, code


async def run_hour(inputs: HourInputs, s: Services, now: datetime) -> dict:
    """Run one hour of on-demand pulses; returns the outcome written to `pulse.json`."""
    hour, _ = start_hour(inputs.start, now)
    deadline = (now + HARD_STOP - REMOVE_LEAD).timestamp()
    try:
        config, plan, code = await _hour_plan(inputs, s, hour, now)
    except FleetError as error:
        await _no_droplets(s.db, hour, f"0 droplets created: {error}", {})
        raise
    outcome = {"hour": hour.isoformat(), "plan": plan.to_dict(), "state": "failed"}
    outcome["code"] = code.record() if code else {"wheel": config.wheel.name}
    hour_root = inputs.root / "hours" / f"{hour:%Y%m%dT%H}"
    hour_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    error = None
    try:
        outcome |= await _pulses(hour_root, config, plan, hour, s, deadline)
        outcome["state"] = _state(outcome)
        if plan.cells and not outcome["created"]:
            causes = ", ".join(f"{k} ({n})" for k, n in outcome["create_failed"].items())
            cause = f"0 of {len(plan.cells)} droplets created: {causes or 'not_created'}"
            await _no_droplets(s.db, hour, cause, {"create_failed": outcome["create_failed"]})
        elif outcome["lost"]:
            lost = ", ".join(outcome["lost"])
            message = f"pulse hour {hour:%Y-%m-%d %H}:00 UTC: cells lost: {lost}"
            await _alert(s.db, hour, "pulse_cells_lost", message, {"cells": outcome["lost"]})
    except asyncio.CancelledError:
        outcome["error"] = "cancelled by a signal"
    except BaseException as caught:  # boundary: an interrupt too; re-raised after the removal
        error, outcome["error"] = caught, type(caught).__name__
    try:
        await _remove_shielded(hour_root, hour, planned_cells(config, plan), s, outcome)
        if outcome["state"] == "done" and outcome["leftover"]:
            outcome["state"] = "partial"
    finally:
        atomic_json(hour_root / "pulse.json", outcome)
    if error is not None:
        raise error
    return outcome


def services(root: Path) -> Services:
    """The manager's: its DigitalOcean token, the fleet's SSH key, issue_token, the orchestrator
    database (fleet.json `database_file`). Read without the fleet checks, so a refused
    `fleet.json` reaches run_hour, which opens `pulse_hour_failed`."""
    from ..onboarding.scheduler import Clock
    from ..orchestrator.db import Database
    from ..orchestrator.nodes import issue_token
    from .collector import MANAGER_ROOT
    from .do_api import DigitalOcean, file_token
    from .fanout import OpenSSHRemote

    raw = json.loads((root / "fleet.json").read_text())
    cloud = DigitalOcean(file_token(MANAGER_ROOT / ".secrets" / "do-token"))
    remote = OpenSSHRemote(root / raw["identity"], root / ".runtime", 0)
    db = Database(str(root / raw["database_file"]))
    return Services(cloud, remote, issue_token, Clock(), db)
