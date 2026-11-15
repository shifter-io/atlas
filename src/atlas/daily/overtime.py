"""DAILY-OVERTIME-1 (#1152, the owner's rule of 2026-11-15): a daily country test runs until it
is finished, whatever the clock says.

An hour's daily run (daily.runner) no longer stops at its slot's end. Its workers keep running
their countries into the next hours; each worker is still removed as soon as its round is
collected (campaign.harvest), and the hour's results are stored when its last worker is done,
for the hour they were scheduled for (estimates.compute, ESTIMATE-SCHED-1). Two stops remain:

- a network with a connection budget (daily.budget) is stopped BUDGET_STOP past its hour, so its
  daily runs end before its pulses open (campaign.pulse_late.GATE_LATEST) and never share its
  connections with them;
- SAFETY past its hour every worker still running is stopped, so no droplet lives forever.

A stop ends the worker's round as `stopped` (SIGTERM to its round unit, `Remote.stop`): the
countries it finished are kept and collected like any finished round, and the country it was
running is left out. The run then has STOP_GRACE to collect them before its time limit
(`limit`) removes whatever is left (daily.runner.clean_up). The hour's workers carry their own
campaign's names and tags, so they never collide with the next hour's.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from ..campaign import pulse_late
from ..campaign.contracts import CampaignSpec, Droplet
from ..campaign.launch import read_hosts
from ..campaign.tick import load_hour
from ..onboarding.manifest import moment
from . import budget

SAFETY = timedelta(hours=6)
STOP_GRACE = timedelta(minutes=15)
BUDGET_STOP = pulse_late.GATE_LATEST - timedelta(minutes=5)
LIVE = ("armed", "running")

log = logging.getLogger(__name__)


def limit(begins: datetime, now: datetime) -> float:
    """Seconds the hour's run may take: until SAFETY plus STOP_GRACE past its hour."""
    return max(0.0, (begins + SAFETY + STOP_GRACE - now).total_seconds())


def stops(begins: datetime, networks) -> list[tuple[datetime, frozenset | None]]:
    """When which workers are stopped, in order: the budgeted networks' (if any) at
    BUDGET_STOP, every worker (None) at SAFETY."""
    held = frozenset(n for n in networks if n in budget.budgets())
    early = [(begins + BUDGET_STOP, held)] if held else []
    return [*early, (begins + SAFETY, None)]


async def stop_live(root, spec: CampaignSpec, remote, networks=None) -> list[str]:
    """Stop the round of every worker of the hour still armed or running (of `networks`, None:
    all); returns their names. A failed stop is logged and leaves its worker to the limit."""
    record = load_hour(root, spec, 0)
    hosts = read_hosts(root, 0)
    stopped = []
    for worker in record.droplets:
        if worker.status not in LIVE or (networks is not None and worker.network not in networks):
            continue
        at = moment(worker.requested_at or record.scheduled_at)
        host = hosts.get(str(worker.droplet_id))
        droplet = Droplet(
            worker.droplet_id, worker.name, worker.region, worker.size, "active", at, (), host
        )
        try:
            await remote.stop(droplet)
        except Exception as error:  # boundary: one worker's stop; the limit still removes it
            log.warning("daily overtime: %s not stopped (%s)", worker.name, type(error).__name__)
            continue
        stopped.append(worker.name)
    return stopped


async def watch(root, spec: CampaignSpec, remote_factory, begins: datetime, clock, outcome) -> None:
    """Stop the hour's workers at their stops (`stops`), recording each stop's names in
    outcome["stopped"]; cancelled when the hour settles first."""
    if spec.synthetic or remote_factory is None:
        return
    remote = remote_factory(0)
    for at, networks in stops(begins, spec.networks):
        await clock.sleep(at.timestamp() - clock.now())
        names = await stop_live(root, spec, remote, networks)
        outcome.setdefault("stopped", []).extend(names)
