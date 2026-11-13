"""REPLAY-FIX-2: the runs each simulated hour of a replay would plan (replay.py --plan-only),
read from the database without writing: its settings, targets, countries and nodes.

The counts follow the planners the hour's steps call: batches.pulse_plan at every
ReplaySteps.plan_times moment (a pulse batch per country pulse_slots.spread places, a load batch
per runner region for a census target), browser_plan with BROWSER-MODEL-1's settings at the
hour's start, and a batch_runs row per participating product of each batch. The participants are
the database's own and the replay's: its networks' products, as its steps register them. On a
database without the replay's batches (a planned slot is not planned again).
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import browser_model
from replay_steps import ReplaySteps, networks, slice_tests

from atlas.browser.contracts import BROWSER_REGION_ID
from atlas.orchestrator.browser import slot_starts
from atlas.orchestrator.control import read_setting
from atlas.orchestrator.planparts import TAKING
from atlas.orchestrator.pulse_slots import GRACE, MIN_GAP, spread

KINDS = ("daily", "pulse", "load", "browser")
TARGETS = (
    "SELECT target_id,pulse_interval_minutes,census FROM targets WHERE pulse_enabled "
    "ORDER BY sort_order,target_id"
)
COUNTRIES = (
    "SELECT trim(c.country_code),c.region_id FROM countries c JOIN runner_regions r "
    "ON r.region_id=c.region_id AND r.enabled WHERE c.enabled"
)
BROWSER_PRODUCTS = "SELECT DISTINCT provider,product FROM nodes WHERE region_id=%s"
PRESENT = "SELECT target_id FROM targets WHERE target_id=ANY(%s)"


class Plan:
    """The planning inputs of one database, and the pulse slots already planned."""

    def __init__(self, conn, steps: ReplaySteps):
        self.steps, self.opened = steps, set()
        on = read_setting(conn, "scheduler.pulse_enabled")
        self.targets = conn.execute(TARGETS).fetchall() if on else []
        self.lead = timedelta(seconds=float(read_setting(conn, "pulse.lead_seconds")))
        self.loading = bool(read_setting(conn, "load.enabled"))
        self.countries = conn.execute(COUNTRIES).fetchall()
        mine = {(name, steps.product(name)) for name in steps.model.settings.networks}
        taking = {(row[1], row[2]) for row in conn.execute(TAKING)}
        self.products, self.browser_runs = len(taking | mine), self.browser(conn)
        self.given = browser_settings(conn)

    def browser(self, conn) -> int:
        """Runs per browser batch: the products with a browser node once the replay's are."""
        names = self.steps.model.settings.networks
        free = [name for name in names if self.steps.ours(BROWSER_REGION_ID, name)]
        if not free:  # the step is skipped
            return 0
        found = set(conn.execute(BROWSER_PRODUCTS, (BROWSER_REGION_ID,)).fetchall())
        return len(found | {(name, self.steps.product(name)) for name in free})

    def pulse_batches(self, moment: datetime) -> tuple[int, int]:
        """The pulse and load batches pulse_plan opens at `moment`."""
        pulses = loads = 0
        regions = len({region for _, region in self.countries})
        for index, (target_id, minutes, census) in enumerate(self.targets):
            every = timedelta(minutes=minutes)
            seconds = every.total_seconds()
            slot = datetime.fromtimestamp(moment.timestamp() // seconds * seconds, UTC)
            if (target_id, slot) in self.opened:
                continue
            self.opened.add((target_id, slot))
            origin = slot if moment - slot <= GRACE else min(moment, slot + every)
            window, offset = slot + every - origin, index / len(self.targets)
            pulses += len(spread(self.countries, origin + self.lead, window, offset, MIN_GAP))
            loads += regions if census and self.loading else 0
        return pulses, loads

    def hour(self, start: datetime, slices: Path) -> dict[str, int]:
        """The runs the hour starting at `start` plans, by kind."""
        pulses = loads = 0
        for moment in self.steps.plan_times(start):
            found = self.pulse_batches(moment)
            pulses, loads = pulses + found[0], loads + found[1]
        targets, present, lead, countries = self.given
        batches = sum(item[0] in present for item in slot_starts(targets, start, lead))
        browser = batches * countries * self.browser_runs
        found = {"daily": len(slice_tests(slices, start.hour)), "pulse": pulses * self.products}
        return found | {"load": loads * self.products, "browser": browser}


def browser_settings(conn) -> tuple:
    """BROWSER-MODEL-1's targets, those present in `targets` (the others are not planned), its
    lead and its number of countries."""
    with tempfile.TemporaryDirectory(prefix="scale-replay-plan-") as temporary:
        given = browser_model.load(browser_model.settings(), Path(temporary))
    present = {row[0] for row in conn.execute(PRESENT, ([t.id for t in given.targets],))}
    targets = [(t.id, t.every_minutes) for t in given.targets]
    lead = timedelta(seconds=given.schedule.lead_seconds)
    return targets, present, lead, len(given.schedule.countries)


def plan(conn, args, starts: list[datetime]) -> dict:
    """The runs of each simulated hour (`starts`) by kind, and their totals."""
    slices = Path(args.slices)
    state = {"replay_id": "plan-only", "networks": networks(slices), "nodes": []}
    counter = Plan(conn, ReplaySteps(conn, args, state))
    hours = [{"start": start.isoformat()} | counter.hour(start, slices) for start in starts]
    totals = {kind: sum(hour[kind] for hour in hours) for kind in KINDS}
    return {"hours": hours, "totals": totals, "days": args.days}


def table(found: dict) -> str:
    """The plan as text: a line per simulated hour, then the totals."""
    lines = [f"{'hour':<25}" + "".join(f"{kind:>9}" for kind in KINDS)]
    for hour in found["hours"]:
        lines.append(f"{hour['start']:<25}" + "".join(f"{hour[k]:>9}" for k in KINDS))
    total = f"total of {found['days']} days"
    lines.append(f"{total:<25}" + "".join(f"{found['totals'][k]:>9}" for k in KINDS))
    return "\n".join(lines) + "\n"
