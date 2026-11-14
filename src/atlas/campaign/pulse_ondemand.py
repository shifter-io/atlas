"""PULSE-ONDEMAND-1: one hour of on-demand pulses on the manager (`daily pulse-hour`).

The pulse root holds the pulse fleet's `fleet.json` (pulse_fleet.load_config: account, images,
key, wheel, orchestrator_url, database_file and each network's product, provider config and
credentials). For the hour the job plans one droplet per network and region (daily.pulses, the
networks of both the formulas and `fleet.json`), refuses the hour over the cap (the droplets
under the pulse tag still up count), then, with its ledger in `<root>/hours/<YYYYMMDDTHH>/`:

1. creates and arms the hour's nodes through the pulse fleet (`Fleet.reconcile` with the hour's
   cells: create under the pulse tag, confirm project Dev Resources, push the node and start it);
2. opens the hour's pulses for the armed cells (orchestrator.pulse_hour.open_hour);
3. waits until every pulse batch of the hour is closed, at most until DONE_BY past the hour;
4. removes every recorded node (pulse_fleet.retire: scope.retire_pulse_node, then read until
   gone), whatever happened before: an error, the limit or a signal (the removal is shielded).

A node not verified gone opens the `pulse_teardown_failed` alert (major, one per hour) and is
listed in `leftover`. The outcome is `<hour root>/pulse.json` (names and counts, no address).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..daily.planner import HourPlan, load_schedules
from ..daily.pulses import DEFAULT_CAP, check_cap, pulse_hour
from ..daily.runner import start_hour
from ..onboarding.files import atomic_json, lock
from ..orchestrator.events import raise_alert
from ..orchestrator.pulse_hour import Armed, open_hour
from . import pulse_fleet
from .contracts import PULSE_TAG
from .pulse_fleet import Fleet, FleetClients, FleetConfig, FleetError, node_name

# Past the hour: the pulses are done or the nodes go anyway. Packed into 40 minutes they end
# by about HH:42; 50 leaves the removal time before the next hour's job starts (HH:57).
DONE_BY = timedelta(minutes=50)
WAIT_POLL_SECONDS = 30.0
OPEN_SQL = (
    "SELECT count(*) AS open FROM test_batches WHERE kind='pulse' AND scheduled_for=%s "
    "AND status IN ('scheduled','running')"
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


def hour_config(config: FleetConfig, plan: HourPlan) -> FleetConfig:
    """The fleet config of the hour: only its cells; every region needs an image."""
    missing = [r for r in plan.regions() if r not in config.image_by_region]
    if missing:
        raise FleetError(f"fleet.json: no image for {', '.join(missing)}")
    cells = tuple((c.network, c.region) for c in plan.cells)
    return replace(config, regions=plan.regions(), cells=cells)


def armed_cells(config: FleetConfig, plan: HourPlan, records: dict) -> list[Armed]:
    """The cells whose node was armed, as open_hour takes them."""
    armed = []
    for cell in plan.cells:
        record = records.get(node_name(cell.network, cell.region))
        if record is not None and record.status == "armed":
            network = config.networks[cell.network]
            armed.append(Armed(network.provider, network.product, cell.region, cell.countries))
    return armed


async def wait_closed(db, hour: datetime, clock) -> int:
    """Poll until no pulse batch of the hour is open or DONE_BY passed; returns those open."""
    deadline = (hour + DONE_BY).timestamp()
    while True:
        async with db.connection() as conn:
            open_ = (await (await conn.execute(OPEN_SQL, (hour,))).fetchone())["open"]
        if not open_ or clock.now() + WAIT_POLL_SECONDS > deadline:
            return open_
        await clock.sleep(WAIT_POLL_SECONDS)


async def remove_all(hour_root: Path, cloud, clock) -> list[dict]:
    """Retire every recorded node not yet gone; returns those not verified gone."""
    left = []
    for name, record in sorted(pulse_fleet.load_nodes(hour_root).items()):
        if not record.droplet_id or record.status == "destroyed":
            continue
        try:
            gone = (await pulse_fleet.retire(hour_root, name, cloud, clock))["gone"]
            reason = None if gone else "not gone"
        except Exception as error:  # boundary: one node's refusal never keeps the others up
            gone, reason = False, type(error).__name__
        if not gone:
            left.append({"name": name, "id": record.droplet_id, "reason": reason})
    return left


async def _alert(db, hour: datetime, left: list[dict]) -> None:
    async with db.transaction() as conn:
        await raise_alert(
            conn,
            "pulse_teardown_failed",
            f"{len(left)} pulse droplets of {hour:%Y-%m-%d %H}:00 UTC are not verified gone",
            severity="major",
            dedupe_key=f"pulse_teardown_failed:{hour:%Y%m%dT%H}",
            data={"hour": hour.isoformat(), "nodes": left},
        )


async def _pulses(hour_root: Path, config, plan: HourPlan, hour: datetime, s: Services) -> dict:
    """Create and arm the hour's nodes, open their pulses and wait for them."""
    with lock(hour_root / ".lock"):
        fleet = Fleet(hour_root, config, FleetClients(s.cloud, s.remote, s.issuer, s.clock))
        report = await fleet.reconcile()
    armed = armed_cells(config, plan, pulse_fleet.load_nodes(hour_root))
    now = datetime.fromtimestamp(s.clock.now(), UTC)
    opened = await open_hour(s.db, hour, armed, now) if armed else {"created": 0}
    still_open = await wait_closed(s.db, hour, s.clock) if opened["created"] else 0
    failed = report["failed"] + report["destroy_refused"]
    return {"armed": len(armed), "failed": failed, "opened": opened, "still_open": still_open}


async def _remove_shielded(hour_root: Path, hour: datetime, s: Services, outcome: dict) -> None:
    """Remove the hour's nodes to the end, a signal never interrupting it; alert on leftovers."""
    removal = asyncio.ensure_future(remove_all(hour_root, s.cloud, s.clock))
    while not removal.done():
        try:
            await asyncio.shield(removal)
        except asyncio.CancelledError:
            outcome["error"] = "cancelled by a signal"
    outcome["leftover"] = removal.result()
    if outcome["leftover"]:
        await _alert(s.db, hour, outcome["leftover"])


def _state(outcome: dict) -> str:
    """done: every armed node's pulses closed; not_opened: the orchestrator opened none."""
    if "skipped" in outcome["opened"]:
        return "not_opened"
    return "timed_out" if outcome["still_open"] else "done"


async def run_hour(inputs: HourInputs, s: Services, now: datetime) -> dict:
    """Run one hour of on-demand pulses; returns the outcome written to `pulse.json`."""
    hour, _ = start_hour(inputs.start, now)
    config = pulse_fleet.load_config(inputs.root)
    schedules = load_schedules(inputs.formulas)
    plan = pulse_hour(schedules, hour.hour, networks=tuple(sorted(config.networks)))
    outcome = {"hour": hour.isoformat(), "plan": plan.to_dict(), "state": "failed"}
    check_cap(plan, inputs.cap, running=len(await s.cloud.list_tag(PULSE_TAG)))
    config = hour_config(config, plan)
    hour_root = inputs.root / "hours" / f"{hour:%Y%m%dT%H}"
    hour_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    error = None
    try:
        outcome |= await _pulses(hour_root, config, plan, hour, s)
        outcome["state"] = _state(outcome)
    except asyncio.CancelledError:
        outcome["error"] = "cancelled by a signal"
    except BaseException as caught:  # boundary: an interrupt too; re-raised after the removal
        error, outcome["error"] = caught, type(caught).__name__
    try:
        await _remove_shielded(hour_root, hour, s, outcome)
    finally:
        atomic_json(hour_root / "pulse.json", outcome)
    if error is not None:
        raise error
    return outcome


def services(root: Path) -> Services:
    """The manager's: its DigitalOcean token, the fleet's SSH key, issue_token, the orchestrator
    database (fleet.json `database_file`)."""
    from ..onboarding.scheduler import Clock
    from ..orchestrator.db import Database
    from ..orchestrator.nodes import issue_token
    from .collector import MANAGER_ROOT
    from .do_api import DigitalOcean, file_token
    from .fanout import OpenSSHRemote

    config = pulse_fleet.load_config(root)
    cloud = DigitalOcean(file_token(MANAGER_ROOT / ".secrets" / "do-token"))
    remote = OpenSSHRemote(config.identity, root / ".runtime", 0)
    db = Database(str(config.database_file))
    return Services(cloud, remote, issue_token, Clock(), db)
