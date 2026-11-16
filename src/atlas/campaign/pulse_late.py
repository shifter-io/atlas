"""PROXY-CHEAP-BUDGET-1: a budgeted network pulses after its daily runs of the hour.

A network with a connection budget (daily.budget) never runs its daily tests and its pulses at
the same time. The hour's pulse job (pulse_ondemand, started at HH:57) creates and arms every
cell as before, so the network's droplets are ready, but opens only the other networks' pulses
at once. Once those are stored (about HH:15 to HH:19) it waits, polling every POLL_SECONDS,
until the network's daily runs of the hour are done (`daily_running` is empty) or GATE_LATEST
past the hour, whichever comes first. Then it opens the network's pulses in their own slot (the
minute it opened, never HH:00, so the batches are the network's own), starting at most
LAST_START past the hour, and waits for them until STOP less REMOVE_LEAD past the hour; every
droplet is removed by about HH:55, before the next hour's job creates the same names at HH:57.

PULSE-STEP-1/2: a network with a daily.budget.PULSE_STEP_SECONDS (proxy-cheap: 30 s) is not
opened on a clock. A run holds its droplet's share over node_pool.RUNS_AT_ONCE, and its time
depends on the droplet's region (16 Nov 07:00, 100 requests at 5: fra1 about 22 s, syd1 63 s
with a p90 of 79 s): on a fixed 30 s clock (a run every 15 s) the far droplets fell behind and 58
of 432 runs waited past batch.start_grace_seconds (180 s) and failed `not_started`. So every
PULL_SECONDS the slot gives each droplet its next run (Puller: its countries in code order, the
hourly targets of each in their order) when fewer than RUNS_AT_ONCE of its runs are open (queued,
running or uploading), starting now, never sooner than the step over the targets after its
previous one (15 s: a fast droplet keeps a country every 30 s), none after last_start (HH:50; a
country not started by then waits for the next hour, `not_opened`). No run waits for
connections, so the grace never fails one; a slow droplet starts fewer, one per freed slot.
The late wait (pulse_wait) runs meanwhile and removes each droplet once it has nothing left to
be given and its runs are stored. fra1, the largest region (59 countries, fewer with the
low-availability skip), takes at most about 30 minutes: from a gate at HH:20, every country
starts by HH:50.

The daily runs of the hour are the one-slot campaigns `<campaigns>/<YYYYMMDDtHH>0000z-*` the
daily runner writes beside the template (`campaigns` is the template link's folder; the job's
`--formulas` is `<daily>/formulas` and the template `<daily>/template`). A network's runs are
done when the hour wrote `daily.json`, its ledger state is done or failed, or every cell of the
network is past pending and running. An hour without such a campaign has no daily run to wait
for. At GATE_LATEST with a run still going the pulses start anyway; the job's outcome lists the
cells (`daily_still_running`) and raises one major alert, `pulse_budget_overlap`.

Cost: the network's droplets (8 x s-1vcpu-2gb-amd, pulse_fleet.DEFAULT_SIZE) live from HH:57
until their pulses are stored, about HH:30 when the daily runs end at HH:15, at most about
HH:55: up to 40 minutes longer than the other networks' (about 5.3 droplet-hours an hour, $2.6 a
day, at most).
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..daily import budget
from ..node_pool import RUNS_AT_ONCE
from ..orchestrator.pulse_hour import Armed, open_hour, open_run, queues
from .pulse_wait import BUSY_SQL, NOTHING, Deadlines, wait_and_remove

GATE_LATEST = timedelta(minutes=40)
LAST_START = timedelta(minutes=50)
STOP = timedelta(minutes=55)
FINISH_BY = timedelta(minutes=52)  # STOP less pulse_ondemand.REMOVE_LEAD: the late wait's end
POLL_SECONDS = 15.0
PULL_SECONDS = 1.0  # how often the paced slot looks for a droplet's free slot (PULSE-STEP-2)
WAITING = ("pending", "running")
FINAL = ("done", "failed")


def campaigns_dir(formulas: Path) -> Path:
    """Where the daily runner writes its hour campaigns: beside the template it runs from."""
    return (formulas.parent / "template").resolve().parent


def _waiting(root: Path, networks: set[str]) -> list[str]:
    """One hour campaign's cells of `networks` not done yet."""
    try:
        spec = json.loads((root / "campaign-spec.json").read_text())
    except (OSError, ValueError):
        return []  # not a campaign the runner finished writing
    due = sorted(networks & set(spec.get("networks") or ()))
    if spec.get("hours") != 1 or not due or (root / "daily.json").exists():
        return []  # the template (24 slots), another network's hour or a finished hour
    try:
        record = json.loads((root / "ledger" / "00" / "hour.json").read_text())
    except (OSError, ValueError):
        return [f"{network}/launching" for network in due]
    if record.get("state") in FINAL:
        return []
    cells = record.get("cells") or {}
    keys = [key for key in cells if key.split("/")[0] in due]
    if record.get("state") in ("planned", "launching") and not keys:
        return [f"{network}/launching" for network in due]
    return sorted(key for key in keys if cells[key] in WAITING)


def daily_running(campaigns: Path, hour: datetime, networks: set[str]) -> list[str]:
    """The hour's daily cells of `networks` still running ("network/region"); [] once done."""
    found: list[str] = []
    for root in sorted(campaigns.glob(f"{hour:%Y%m%dt%H}0000z-*")):
        found += _waiting(root, networks)
    return found


async def wait_for_daily(clock, campaigns: Path, hour: datetime, networks: set[str]) -> dict:
    """Wait until the networks' daily runs of the hour are done or GATE_LATEST past it."""
    latest = (hour + GATE_LATEST).timestamp()
    while True:
        running = daily_running(campaigns, hour, networks)
        now = clock.now()
        if not running or now >= latest:
            at = datetime.fromtimestamp(now, UTC).isoformat()
            if not running:
                return {"gate": "daily_done", "at": at}
            return {"gate": "latest", "at": at, "daily_still_running": running}
        await clock.sleep(min(POLL_SECONDS, latest - now))


def slot(hour: datetime, now: datetime) -> datetime:
    """The late batches' scheduled_for: the minute now, at least one minute past the hour."""
    minutes = max(1, min(59, int((now - hour).total_seconds() // 60)))
    return hour + timedelta(minutes=minutes)


def last_start(step: timedelta | None) -> timedelta:
    """The latest start past the hour: LAST_START, earlier when a step's run would end after
    FINISH_BY."""
    return min(LAST_START, FINISH_BY - step) if step else LAST_START


async def open_late(db, clock, hour: datetime, cells: list[Armed]) -> dict:
    """Open the held cells' pulses in their own slot at once, one country every
    pulse.step_seconds, starting by LAST_START past the hour (a network without a step)."""
    now = datetime.fromtimestamp(clock.now(), UTC)
    found = slot(hour, now)
    opened = await open_hour(db, hour, cells, now, slot=found, last_start=LAST_START)
    return opened | {"slot": found.isoformat()}


class Puller:
    """PULSE-STEP-2: which droplet gets its next run now. `runs`: each node's runs in order;
    `gap`: the least time between two runs of one node; `last`: the latest start."""

    def __init__(self, runs: dict, gap: timedelta, last: datetime) -> None:
        self.runs = {node: list(left) for node, left in runs.items() if left}
        self.pulling = set(self.runs)  # the nodes still to be given a run (pulse_wait keeps them)
        self.gap, self.last = gap, last
        self.after: dict = {}  # each node's earliest next start
        self.not_opened = 0

    def due(self, now: datetime, busy: Counter) -> list[tuple]:
        """The (node, run) pairs to open at `now`, given each node's open runs (`busy`)."""
        found = []
        for node in sorted(self.pulling):
            left = self.runs[node]
            if left and now > self.last:
                self.not_opened += len(left)
                left.clear()
            if not left:  # its last run was opened at an earlier call: it is busy by now
                self.pulling.discard(node)
            elif busy[node] < RUNS_AT_ONCE and now >= self.after.get(node, now):
                found.append((node, left.pop(0)))
                self.after[node] = now + self.gap
        return found


async def _busy(db, hour: datetime) -> Counter:
    """The hour's runs not done of each node (provider, product, region)."""
    async with db.connection() as conn:
        cursor = await conn.execute(BUSY_SQL, (hour, hour + timedelta(hours=1)))
        rows = await cursor.fetchall()
    return Counter((r["provider"], r["product"], r["region"]) for r in rows)


async def pull(db, clock, found: datetime, puller: Puller) -> dict:
    """Give each droplet its runs in `found` (the slot) as the Puller says until none is left."""
    hour = found.replace(minute=0)
    created = runs = 0
    while puller.pulling:
        now = datetime.fromtimestamp(clock.now(), UTC)
        for node, run in puller.due(now, await _busy(db, hour)):
            made, added = await open_run(db, found, node, run, now)
            created, runs = created + made, runs + added
        if puller.pulling:
            await clock.sleep(PULL_SECONDS)
    return {"created": created, "runs": runs, "not_opened": puller.not_opened}


async def open_and_wait(hour_root: Path, hour: datetime, cells: tuple, s, networks) -> dict:
    """Open the held cells' pulses (`cells`: the held nodes by name and the cells to open) and
    wait for them until FINISH_BY, removing each node once its runs are stored: at once for a
    network without a step, else run by run as each droplet has a free slot (Puller)."""
    nodes, held = cells
    limits = Deadlines((hour + FINISH_BY).timestamp(), (hour + FINISH_BY).timestamp())
    step = budget.pulse_step(networks)
    if step is None:
        opened = await open_late(s.db, s.clock, hour, held)
        left = NOTHING
        if opened["created"] or opened.get("runs"):
            left = await wait_and_remove(hour_root, hour, nodes, s, limits)
        return {"opened": opened} | left
    runs, least, targets = await queues(s.db, held)
    step = max(step, least)
    found = slot(hour, datetime.fromtimestamp(s.clock.now(), UTC))
    puller = Puller(runs, step / max(1, targets), hour + last_start(step))
    waiting = asyncio.create_task(
        wait_and_remove(hour_root, hour, nodes, s, limits, pulling=puller.pulling)
    )
    try:
        opened = await pull(s.db, s.clock, found, puller)
    except BaseException:  # an error (or a stop) ends the wait too; the error is the caller's
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        raise
    paced = {"slot": found.isoformat(), "step_seconds": step.total_seconds()}
    return {"opened": opened | paced} | await waiting


def keys(config) -> set[tuple[str, str]]:
    """(provider, product) of the fleet's networks the hour holds back (FleetConfig)."""
    return {
        (config.networks[name].provider, config.networks[name].product)
        for name in config.max_connections
    }
