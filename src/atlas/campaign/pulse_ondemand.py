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
3. waits until no run of the hour is left to store, retiring and removing at once each armed
   node whose runs of the hour are all stored (PULSE-COMPACT-1): a droplet lives about 15 to 20
   minutes. A run still queued or running at HARD_STOP after the job started less REMOVE_LEAD
   ends the wait (timed_out); runs only uploading then are waited for until STORE_BY past the
   hour (PULSE-STATE-1, below);
4. retires the nodes of every planned cell (orchestrator.pulse_hour.retire_nodes), so the
   manager's offline job raises no alert for them, then removes every recorded node left
   (pulse_removal.retire_many: the guarded path, 16 at once), whatever happened before: an
   error, the limit or a signal (both are shielded; PULSE-NODE-RETIRE-1).

The cap counts only the droplets under the pulse tag younger than LEFTOVER_AGE; an older one is
a leftover of a finished hour: never counted, removed through the hour ledger that records it,
and the leftovers share one major alert, `pulse_leftover`, one per hour (PULSE-COMPACT-1).

A node not verified gone opens the `pulse_teardown_failed` alert (major, one per hour) and is
listed in `leftover`. An hour that created none of its droplets (every create failed, or
`fleet.json` or the cap refused it) opens `pulse_hour_failed` (major, one per hour;
PULSE-FLEET-FIX-1; pulse_alerts has every alert of the hour). The
outcome is `<hour root>/pulse.json` (names and counts, no address): `created`, `create_failed`
(the count by error class) and each failed node's status, with the class and DigitalOcean's
message for a failed create. A planned cell with no armed node at the end has its countries
handed to another region's droplet of its network (`handed`); one whose network has none is
`lost` ("network/region"), and the lost cells share one major alert, `pulse_cells_lost`.
`state` (_state): done, partial (cells lost or droplets left over), timed_out (a batch still had
a run queued or running at the run limit: `still_open`), not_opened or failed.

PULSE-STATE-1: the hourly calculation (orchestrator jobs.py `calculate`, from about HH:06 for 6
to 17 minutes) holds the ingest lock, so an upload arriving then waits behind it as an
`uploading` run: its data is on the manager, only not stored yet. Once no run is queued or
running, the job waits for those stores until STORE_BY past the hour (each droplet stays until
its own runs are stored, so an upload the manager refused can still be sent again), then removes
what is left: the hour is done, `waiting_to_store` counting the batches still uploading. Only
real loss (cells lost, runs not uploaded by the limit, droplets left over, nothing opened)
makes the hour not done, opens its alert and exits 3.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..daily.planner import HourPlan, load_schedules
from ..daily.pulses import DEFAULT_CAP, CapError, check_cap, pulse_hour
from ..daily.runner import start_hour
from ..onboarding.files import atomic_json
from ..orchestrator.pulse_hour import Armed, open_hour, retire_nodes
from . import current_wheel, pulse_fleet, pulse_replace
from .pulse_alerts import alert, hour_alerts, hour_failed
from .contracts import PULSE_TAG, with_legacy
from .current_wheel import CurrentWheel
from .pulse_fleet import FleetConfig, FleetError, node_name
from .pulse_removal import remove_all, remove_leftovers, retire_many

# A droplet's hard stop: created from HH:57, gone by HH+1:22 (the wait ends REMOVE_LEAD before),
# unless its runs are only waiting behind the hourly calculation to be stored: then by STORE_BY
# past the hour (HH+1:50, before the next hour's droplets at HH+1:57 and the cap's LEFTOVER_AGE).
HARD_STOP, REMOVE_LEAD = timedelta(minutes=25), timedelta(minutes=3)
STORE_BY = timedelta(minutes=50)
LEFTOVER_AGE = timedelta(minutes=30)  # older at an hour's start: a finished hour's leftover
WAIT_POLL_SECONDS = 15.0
# The runs of the hour's open batches not stored yet, with their node (provider, product and
# the node's region, which a handed-off country's batch does not share): `uploading` ones are
# with the manager already (PULSE-REPLACE-1).
BUSY_SQL = (
    "SELECT b.batch_id,br.status,n.provider,n.product,r.slug AS region FROM batch_runs br "
    "JOIN test_batches b USING(batch_id) JOIN nodes n ON n.node_id=br.node_id "
    "JOIN runner_regions r ON r.region_id=n.region_id WHERE b.kind='pulse' "
    "AND b.scheduled_for=%s AND b.status IN ('scheduled','running') "
    "AND br.status NOT IN ('done','failed','skipped','killed')"
)


@dataclass(frozen=True)
class Deadlines:
    runs: float  # a run still queued or running then ends the wait: timed_out
    stores: float  # runs only uploading (waiting to be stored) are waited for until then


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


def _left(rows: list) -> dict:
    """The batches with a run still queued or running (`still_open`) and their nodes, and those
    whose runs only wait for the manager to store them (`waiting_to_store`)."""
    running = [r for r in rows if r["status"] in ("queued", "running")]
    open_ = {r["batch_id"] for r in running}
    return {
        "still_open": len(open_),
        "open_nodes": sorted({f"{r['provider']}/{r['region']}" for r in running}),
        "waiting_to_store": len({r["batch_id"] for r in rows} - open_),
    }


async def wait_and_remove(
    hour_root: Path, hour: datetime, armed: dict[str, Armed], s: Services, limits: Deadlines
) -> dict:
    """Poll until no run of the hour is left to store, a run is still queued or running at the
    run limit, or the store limit; at every poll remove each armed node with none left, once
    (the final removal retries a failure). Returns _left's counts and the nodes removed."""
    tried: dict[str, str | None] = {}
    while True:
        async with s.db.connection() as conn:
            rows = await (await conn.execute(BUSY_SQL, (hour,))).fetchall()
        busy = {(r["provider"], r["product"], r["region"]) for r in rows}
        left = _left(rows)
        done = {
            name: cell
            for name, cell in armed.items()
            if name not in tried and (cell.provider, cell.product, cell.region) not in busy
        }
        if done:
            tried |= await remove_done(hour_root, done, s)
        limit = limits.runs if left["still_open"] else limits.stores
        if not rows or s.clock.now() + WAIT_POLL_SECONDS > limit:
            return left | {"removed": [name for name, reason in tried.items() if reason is None]}
        await s.clock.sleep(WAIT_POLL_SECONDS)


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
    once, the others after their one replacement each, with the countries of the cells still
    without a node handed to another region's droplet of their network (pulse_replace)."""
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
    extra, handed, lost = pulse_replace.hand_off(plan, armed)
    await _open(s, hour, [c for c in armed.values() if c.region in held] + extra, results)
    found = {"replaced": replaced, "handed": handed, "lost": lost}
    return report, records, armed, pulse_replace.merge_opened(results), found


async def _pulses(
    hour_root: Path, config, plan: HourPlan, hour: datetime, s: Services, limits: Deadlines
) -> dict:
    """Create and arm the hour's nodes, open their pulses and remove each as it is done."""
    report, records, armed, opened, found = await _arm_and_open(hour_root, config, plan, hour, s)
    left = {"still_open": 0, "open_nodes": [], "waiting_to_store": 0, "removed": []}
    if opened["created"] or opened.get("runs"):
        left = await wait_and_remove(hour_root, hour, armed, s, limits)
    removed = left.pop("removed")
    names = [node_name(c.network, c.region) for c in plan.cells]
    armed_names = {n for n, r in records.items() if r.status == "armed"}
    failed = [_failed(n, records) for n in names if n not in armed_names]
    classes = Counter(f["error_class"] or "unknown" for f in failed if "error_class" in f)
    return {
        "armed": len(armed),
        **found,
        "created": sum(1 for n in names if n in records and records[n].droplet_id),
        "create_failed": dict(sorted(classes.items())),
        "failed": failed,
        "unknown": report["unknown"],
        "opened": opened,
        **left,
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
        await alert(s.db, hour, "pulse_teardown_failed", message, {"nodes": left})


def _state(outcome: dict) -> str:
    """The hour's final state (PULSE-REPLACE-1): not_opened, the orchestrator opened none;
    timed_out, a batch still had a run queued or running at the run limit; partial, cells lost
    (`lost`) or droplets not verified gone (`leftover`); else done (every run stored, or still
    waiting for the manager to store it at the store limit: `waiting_to_store`)."""
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
        await alert(s.db, hour, "pulse_leftover", message, {"nodes": swept})
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


def _limits(hour: datetime, now: datetime) -> Deadlines:
    runs = (now + HARD_STOP - REMOVE_LEAD).timestamp()
    return Deadlines(runs, max(runs, (hour + STORE_BY).timestamp()))


def _finish(outcome: dict) -> None:
    """The final state after the removal. `retired` (the nodes the final retire found not
    retired yet: those still up at the end, or a planned cell's node row an earlier hour left)
    is kept only for an hour that is not done: there it names nodes held by runs not stored."""
    if outcome["state"] == "done" and outcome["leftover"]:
        outcome["state"] = "partial"
    if outcome["state"] == "done":
        outcome.pop("retired", None)


async def run_hour(inputs: HourInputs, s: Services, now: datetime) -> dict:
    """Run one hour of on-demand pulses; returns the outcome written to `pulse.json`."""
    hour, _ = start_hour(inputs.start, now)
    try:
        config, plan, code = await _hour_plan(inputs, s, hour, now)
    except (FleetError, CapError) as error:
        await hour_failed(s.db, hour, f"0 droplets created: {error}", {})
        raise
    outcome = {"hour": hour.isoformat(), "plan": plan.to_dict(), "state": "failed"}
    outcome["code"] = code.record() if code else {"wheel": config.wheel.name}
    hour_root = inputs.root / "hours" / f"{hour:%Y%m%dT%H}"
    hour_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    error = None
    try:
        outcome |= await _pulses(hour_root, config, plan, hour, s, _limits(hour, now))
        outcome["state"] = _state(outcome)
        await hour_alerts(s.db, hour, len(plan.cells), outcome)
    except asyncio.CancelledError:
        outcome["error"] = "cancelled by a signal"
    except BaseException as caught:  # boundary: an interrupt too; re-raised after the removal
        error, outcome["error"] = caught, type(caught).__name__
    try:
        await _remove_shielded(hour_root, hour, planned_cells(config, plan), s, outcome)
        _finish(outcome)
        if outcome["state"] == "failed" and error is None:  # a signal: exit 3, so one alert
            await hour_failed(s.db, hour, f"stopped: {outcome['error']}", {})
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
