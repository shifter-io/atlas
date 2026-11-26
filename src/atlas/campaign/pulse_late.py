"""PROXY-CHEAP-BUDGET-1: a budgeted network pulses after its daily runs of the hour.

A network with a connection budget (daily.budget) never runs its daily tests and its pulses at
the same time. The hour's pulse job (pulse_ondemand, started at HH:57) creates, arms and opens
the other networks' cells at once and holds the network's cells back (`split`). Once the others'
pulses are stored (about HH:15 to HH:19) it waits, polling every POLL_SECONDS, until the
network's daily runs of the hour are done and stored (`daily_waiting`) or GATE_LATEST past
the hour, whichever comes first. PULSE-LATE-START-1 (owner decision of 2026-11-16): only then are
the network's droplets created and armed (`arm_late`: the same create, arming, one replacement
and hand-off as the others'), so none sits idle from HH:57 waiting for the gate. Then it opens
the network's pulses in their own slot (the minute it opened, never HH:00, so the batches are
the network's own), starting at most last_start past the hour (LAST_START, HH:50; HH:48 for a
paced network, HH:46 at 2 connections a run), and waits for them until STOP less REMOVE_LEAD
(FINISH_BY, HH:52) past it; every droplet is removed by about HH:55, before the next hour's job
creates the same names at HH:57.

PULSE-STEP-1/2: a network with a daily.budget.PULSE_STEP_SECONDS (proxy-cheap: 30 s) is not
opened on a clock. A run holds its droplet's share over node_pool.RUNS_AT_ONCE, and its time
depends on the droplet's region (16 Nov 07:00, 100 requests at 5: fra1 about 22 s, syd1 63 s
with a p90 of 79 s): on a fixed 30 s clock (a run every 15 s) the far droplets fell behind and 58
of 432 runs waited past batch.start_grace_seconds (180 s) and failed `not_started`. So every
PULL_SECONDS the slot gives each droplet its next run (Puller: its countries in code order, the
hourly targets of each in their order) when fewer than RUNS_AT_ONCE of its runs are open (queued,
running or uploading), starting now, never sooner than the step over the targets after its
previous one (15 s: a fast droplet keeps a country every 30 s), none after last_start (a
country not started by then waits for the next hour, `not_opened`). No run waits for
connections, so the grace never fails one; a slow droplet starts fewer, one per freed slot.
The late wait (pulse_wait) runs meanwhile and removes each droplet once it has nothing left to
be given and its runs are stored. fra1, the largest region (59 countries, fewer with the
low-availability skip), takes at most about 30 minutes: from a gate at HH:18, every country
starts by HH:48.

PULSE-STEP-3: a paced run must end before FINISH_BY, so its last start is PACED_ROOM (4 minutes)
earlier, HH:48, not LAST_START. On 16 Nov 12:00 nyc3 (about a minute a run) started one at
12:49:51 that was still running at 12:52 (over 131 s) and failed `node_removed`; 4 minutes hold
the work poll (5 s) and a run of 235 s, about three times the slowest region's p90 (syd1, 79 s).
A run that reaches its own hard deadline (node_pace.run_seconds, 330 s at 5 connections) still
alerts.

PC-BUDGET-1: proxy-cheap's pulse droplets hold 4 connections, 2 per run (daily.budget), so a run
takes about 2.5 times as long as at 5: syd1's p90 about 200 s, inside PACED_ROOM. Its hard
deadline grows the same way (780 s), so a run whose requests mostly time out is more often still
running at HH:52 and lost (`pulse_runs_open`). A large region's droplet starts only some of its
countries and stays busy until about HH:50: up to about 37 minutes from a gate at HH:15.

PULSE-ROOM-1 (#1412): at 2 connections a run, 4 minutes were not enough. From 19 Nov 12:00 to
23 Nov 17:00 proxy-cheap finished 18,769 pulse runs (p99 by region 189 to 356 s, tor1's 455 s
on only 209 runs; the longest 483 s) and lost 36 to `node_removed`, every one started from
HH:45:59 to HH:48:03 and still running at HH:52 after 240 to 364 s (nyc3, sgp1, fra1, lon1,
syd1, blr1, ams3). So a run of fewer than budget.MIN_RUN connections (`room`) gets NARROW_ROOM,
6 minutes, and starts by HH:46: 83 of the 18,769 ran longer (381 over 4 minutes); 35 of the 36
would have waited for the next hour instead of being lost. The cost: the starts of HH:46 to
HH:48 (about 12 runs an hour, the last countries of the slow regions in code order) wait for the
next hour too (`not_opened`). Runs of 5 connections or more keep PACED_ROOM.

The daily runs of the hour are the one-slot campaigns `<campaigns>/<YYYYMMDDtHH>0000z-*` the
daily runner writes beside the template (`campaigns` is the template link's folder; the job's
`--formulas` is `<daily>/formulas` and the template `<daily>/template`). A network's runs are
done when the hour wrote `daily.json`, its ledger state is done or failed, or every cell of the
network is past pending and running. An hour without such a campaign has no daily run to wait
for. At GATE_LATEST with a run still going the droplets are created and their pulses start
anyway; the job's outcome lists the cells (`daily_still_running`) and raises one major alert,
`pulse_budget_overlap`.

PULSE-DROPLET-AGE-1 (#1306): the gate also waits until the network's ended cells are stored,
each listed in the hour's `daily-ingest.json` (daily.ingest lists an import part's cells once
the whole part is stored: the network alone when it ended early, else with the hour's rest; an
import that fails lists none, so the gate waits until GATE_LATEST). Until then a pulse cannot
start working: storing a run takes its network's lock (storage.NETWORK_LOCK), which the
network's daily import holds for its whole store, and a node frees a run's connections only
once its upload is stored, so a droplet created before fills its two run slots and idles
(17 Nov 05:00: created about 05:12, pulses from 05:39, 3 droplets lived 31 to 36 minutes). At
GATE_LATEST the droplets are created all the same, with the cells still storing in
`daily_not_stored` (no alert: the import uses no connections).

PULSE-GATE-1 (#1414): a cell runs until its worker's round ends, by the worker's own round
state (its `finished_at` in the hour's ledger, set by campaign.harvest once the round unit
ended), not until its results are pulled: the cell's ledger value stays `running` until a pull
succeeds, and a cut transfer is retried every harvest.PULL_RETRY_SECONDS (5 minutes). So a cell
stopped at its budget whose pull has not completed is ended but not stored (`daily_not_stored`,
no alert). On 20 Nov 08:00 a cell stopped at 08:27, its first pull failed and the second ended
at 08:39: the gate counted it as still running and raised a false `pulse_budget_overlap`. The
alert names both lists: the cells still running and those ended but not stored yet.

Timing and cost (BUDGET-STOP-EARLIER-1, #1293): GATE_LATEST is HH:30, so the network's daily
cells stop at HH:27 (daily.overtime.BUDGET_STOP) and its daily droplet, created at about HH:58:30
of the hour before, lives under 30 minutes with its teardown (with the stop at HH:35 it lived up
to 36.9 minutes, 16 Nov). Droplets created at the gate (one boots in about 16 s, #1126; the
others', created at HH:57, start their pulses from about HH:00) start their pulses by about
HH:32. From HH:32 to HH:48 (last_start) a fast droplet (a run every 15 s, PULSE-STEP-2) starts
about 64 runs, a slow one (syd1, 63 s a run, two at once) about 30 (at 2 connections a run,
PC-BUDGET-1: about 34 and 12); the rest of its countries wait for the next hour (`not_opened`).
The network's 8 droplets (s-1vcpu-2gb-amd, pulse_fleet.DEFAULT_SIZE) live from the gate until
their pulses are stored: about 15 minutes after a gate at HH:15, at most about 25 (HH:30 to
HH:55) at 5 connections a run.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..daily import budget
from ..daily.planner import HourPlan
from ..node_pool import RUNS_AT_ONCE
from ..orchestrator.db import reconnecting
from ..orchestrator.pulse_hour import Armed, open_hour, open_run, queues
from . import pulse_fleet, pulse_replace
from .pulse_fleet import FleetConfig
from .pulse_wait import NOTHING, Deadlines, busy_rows, wait_and_remove

GATE_LATEST = timedelta(minutes=30)  # the held droplets' creation; their pulses start by ~HH:32
LAST_START = timedelta(minutes=50)
STOP = timedelta(minutes=55)
FINISH_BY = timedelta(minutes=52)  # STOP less pulse_ondemand.REMOVE_LEAD: the late wait's end
PACED_ROOM = timedelta(minutes=4)  # a paced run's time before FINISH_BY (PULSE-STEP-3)
NARROW_ROOM = timedelta(minutes=6)  # the same for a run under MIN_RUN connections (PULSE-ROOM-1)
POLL_SECONDS = 15.0
PULL_SECONDS = 1.0  # how often the paced slot looks for a droplet's free slot (PULSE-STEP-2)
WAITING = ("pending", "running")
INGEST_REPORT = "daily-ingest.json"  # daily.ingest.REPORT: the cells the hour's import stored
FINAL = ("done", "failed")


def campaigns_dir(formulas: Path) -> Path:
    """Where the daily runner writes its hour campaigns: beside the template it runs from."""
    return (formulas.parent / "template").resolve().parent


def _stored(root: Path) -> set[str]:
    """The cells ("network/region") the hour's daily import stored (its daily-ingest.json)."""
    try:
        return set(json.loads((root / INGEST_REPORT).read_text()).get("cells") or ())
    except (OSError, ValueError, AttributeError):
        return set()


def _rounds_ended(record: dict) -> set[str]:
    """The cells ("network/region") whose worker's round has ended, by the worker's own round
    state (campaign.harvest sets its `finished_at` once the round unit ended, before the pull
    that marks the cell; PULSE-GATE-1). Any of a cell's workers ends it: only a worker that ran
    its round has one, and a replacement comes only from a failed launch, before any round."""
    workers = record.get("droplets") or ()
    return {
        f"{worker.get('network')}/{worker.get('region')}"
        for worker in workers
        if isinstance(worker, dict) and worker.get("finished_at")
    }


def _waiting(root: Path, networks: set[str]) -> tuple[list[str], list[str]]:
    """One hour campaign's cells of `networks` still running, and those ended but not stored."""
    try:
        spec = json.loads((root / "campaign-spec.json").read_text())
    except (OSError, ValueError):
        return [], []  # not a campaign the runner finished writing
    due = sorted(networks & set(spec.get("networks") or ()))
    if spec.get("hours") != 1 or not due:
        return [], []  # the template (24 slots) or another network's hour
    ended = (root / "daily.json").exists()
    try:
        record = json.loads((root / "ledger" / "00" / "hour.json").read_text())
    except (OSError, ValueError):
        return ([], []) if ended else ([f"{network}/launching" for network in due], [])
    cells = record.get("cells") or {}
    keys = [key for key in cells if key.split("/")[0] in due]
    ended = ended or record.get("state") in FINAL
    if not ended and record.get("state") in ("planned", "launching") and not keys:
        return [f"{network}/launching" for network in due], []
    over = _rounds_ended(record)
    running = [] if ended else sorted(k for k in keys if cells[k] in WAITING and k not in over)
    stored = _stored(root)
    return running, sorted(key for key in keys if key not in running and key not in stored)


def daily_waiting(campaigns: Path, hour: datetime, networks: set[str]) -> tuple[list, list]:
    """The hour's daily cells of `networks` ("network/region") still running, and those ended
    but not stored yet (PULSE-DROPLET-AGE-1); both [] once the network's daily is stored."""
    running: list[str] = []
    storing: list[str] = []
    for root in sorted(campaigns.glob(f"{hour:%Y%m%dt%H}0000z-*")):
        found = _waiting(root, networks)
        running, storing = running + found[0], storing + found[1]
    return running, storing


async def wait_for_daily(clock, campaigns: Path, hour: datetime, networks: set[str]) -> dict:
    """Wait until the networks' daily runs of the hour are done and stored, or GATE_LATEST past
    it."""
    latest = (hour + GATE_LATEST).timestamp()
    while True:
        running, storing = daily_waiting(campaigns, hour, networks)
        now = clock.now()
        if not (running or storing) or now >= latest:
            gate = {"gate": "latest" if running or storing else "daily_done"}
            gate["at"] = datetime.fromtimestamp(now, UTC).isoformat()
            if running:
                gate["daily_still_running"] = running
            return gate | ({"daily_not_stored": storing} if storing else {})
        await clock.sleep(min(POLL_SECONDS, latest - now))


def slot(hour: datetime, now: datetime) -> datetime:
    """The late batches' scheduled_for: the minute now, at least one minute past the hour."""
    minutes = max(1, min(59, int((now - hour).total_seconds() // 60)))
    return hour + timedelta(minutes=minutes)


def last_start(step: timedelta | None, room: timedelta = PACED_ROOM) -> timedelta:
    """The latest start past the hour: LAST_START without a step, else `room` (or the step, if
    longer) before FINISH_BY, so a paced run ends before the late wait does (PULSE-STEP-3)."""
    return min(LAST_START, FINISH_BY - max(step, room)) if step else LAST_START


def room(connections: dict[str, int]) -> timedelta:
    """NARROW_ROOM when a run on the networks' pulse droplets (`connections`: each droplet's,
    a run holding its share over RUNS_AT_ONCE, node_pool) has fewer than budget.MIN_RUN
    connections, else PACED_ROOM (PULSE-ROOM-1)."""
    runs = [max(1, each // RUNS_AT_ONCE) for each in connections.values()]
    return NARROW_ROOM if runs and min(runs) < budget.MIN_RUN else PACED_ROOM


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


async def _busy(db, clock, hour: datetime) -> Counter:
    """The hour's runs not done of each node (provider, product, region)."""
    rows = await reconnecting(clock, busy_rows, db, hour)
    return Counter((r["provider"], r["product"], r["region"]) for r in rows)


async def pull(db, clock, found: datetime, puller: Puller) -> dict:
    """Give each droplet its runs in `found` (the slot) as the Puller says until none is left."""
    hour = found.replace(minute=0)
    created = runs = 0
    while puller.pulling:
        now = datetime.fromtimestamp(clock.now(), UTC)
        for node, run in puller.due(now, await _busy(db, clock, hour)):
            made, added = await reconnecting(clock, open_run, db, found, node, run, now)
            created, runs = created + made, runs + added
        if puller.pulling:
            await clock.sleep(PULL_SECONDS)
    return {"created": created, "runs": runs, "not_opened": puller.not_opened}


async def open_and_wait(hour_root: Path, hour: datetime, cells: tuple, s, connections) -> dict:
    """Open the held cells' pulses (`cells`: the held nodes by name and the cells to open) and
    wait for them until FINISH_BY, removing each node once its runs are stored: at once for a
    network without a step, else run by run as each droplet has a free slot (Puller), none
    after last_start with the room of the networks' `connections` per droplet."""
    nodes, held = cells
    limits = Deadlines((hour + FINISH_BY).timestamp(), (hour + FINISH_BY).timestamp())
    step = budget.pulse_step(connections)
    if step is None:
        opened = await reconnecting(s.clock, open_late, s.db, s.clock, hour, held)
        left = NOTHING
        if opened["created"] or opened.get("runs"):
            left = await wait_and_remove(hour_root, hour, nodes, s, limits)
        return {"opened": opened} | left
    runs, least, targets = await reconnecting(s.clock, queues, s.db, held)
    step = max(step, least)
    found = slot(hour, datetime.fromtimestamp(s.clock.now(), UTC))
    last = hour + last_start(step, room(connections))
    puller = Puller(runs, step / max(1, targets), last)
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
    paced["last_start"] = last.isoformat()
    return {"opened": opened | paced} | await waiting


def split(config: FleetConfig, plan: HourPlan) -> tuple[tuple, tuple]:
    """(config, plan) of the cells armed at the hour's start and of the cells held back, those
    of the networks with a budget share (`config.max_connections`; PULSE-LATE-START-1)."""

    def part(held: bool) -> tuple[FleetConfig, HourPlan]:
        found = replace(plan, cells=tuple(c for c in plan.cells if (c.network in budgeted) == held))
        cells = tuple((c.network, c.region) for c in found.cells)
        return replace(config, cells=cells, regions=found.regions()), found

    budgeted = set(config.max_connections)
    return part(False), part(True)


async def arm_late(hour_root: Path, held: tuple, s) -> dict:
    """Create and arm the held cells' nodes (`held`: their config and plan) once the gate
    opened: a node that never booted or started is replaced once, and a cell still without one
    hands its countries to its network's armed droplet in another region (pulse_replace). The
    armed nodes by name (`armed`), the cells to open (`cells`) and the replaced, handed and lost
    cells."""
    config, plan = held
    error, replaced = None, []
    try:
        await pulse_replace.arm(hour_root, config, s)
        failing = pulse_replace.boot_failed(config, pulse_fleet.load_nodes(hour_root))
        if failing:
            replaced = await pulse_replace.replace_once(hour_root, config, failing, s)
    except Exception as caught:  # boundary: the hour's other results and removal go on
        error = type(caught).__name__
    armed = pulse_replace.armed_cells(config, plan, pulse_fleet.load_nodes(hour_root))
    extra, handed, lost = pulse_replace.hand_off(plan, armed)
    found = {"armed": armed, "cells": [*armed.values(), *extra], "replaced": replaced}
    return found | {"handed": handed, "lost": lost} | ({"arm_error": error} if error else {})
