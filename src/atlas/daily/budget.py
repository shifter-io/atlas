"""PROXY-CHEAP-BUDGET-1/2: a network's budget of simultaneous connections in one hour, shared by
its daily runs, pulses and browser tests across all regions (owner decisions 2026-11-15).

proxy-cheap holds few connections at once (live sweep 2026-11-15 05:03-05:12Z: 50-100 at once
95-99% answered, 250 about 84% with 503s and slow answers, 500 about 39% with HTTP 429; about
30-40 successful requests a second at most). Its first hour on a budget of 250 (16:00Z, 3 daily
droplets x 80) answered 19%, mostly HTTP 503, so its budget is 100 and each droplet holds at
most 30 (PROXY-CHEAP-BUDGET-2). Everything that tests a network listed in CONNECTION_BUDGET
within one hour shares that many connections; BROWSER_RESERVE of them stay free for its browser
tests. The planners split the rest by what runs at the same time, each droplet at most the
network's DROPLET_CAP:

- the daily phase (daily.runner): the hour's n droplets of the network, one per region with a
  country due (planner.plan_hour), each run at floor((budget - reserve) / n), at most the cap
  and the template's concurrency. A worker runs its countries one after another, so the hour
  holds at most n times that.
- the pulse phase (`pulse`, campaign.pulse_ondemand): each of the hour's pulse droplets of the
  network holds at most floor((budget - reserve) / droplets) connections at once, at most the
  cap (`atlas node --max-connections`, node_pool.py), after the daily runs
  (campaign.pulse_late).

A share below MIN_RUN would not fit the budget and is refused (`BudgetError`): with 8 regions
proxy-cheap's share is 11. Each daily country test of a budgeted network makes at most its
COUNTRY_REQUESTS requests (`capped`, DAILY-BUDGET-STOP-1), so a big country never runs half an
hour at its small share (proxy-cheap's Brazil ran about 13 requests a second on 22 connections:
15,000 is about 20 minutes). A network not listed keeps its settings, byte for byte. A budget
changes in code (a pull request and a deploy); docs/campaign.html has the runbook.

PULSE-STEP-1: a budgeted network's pulses are the same 100 requests as every network's (owner
rule of 2026-11-16, a fair ranking), every country every hour, paced to its share: its late slot
(campaign.pulse_late) starts one country every PULSE_STEP_SECONDS on each droplet instead of
pulse.step_seconds (12), the two targets half a step apart. A run holds its droplet's share over
node_pool.RUNS_AT_ONCE (proxy-cheap: 11 // 2 = 5) and 100 requests at 5 took about 29 s (15 Nov
2026), so at 30 s a run starts every 15 s and overlaps only the one before it: the droplet never
waits for connections and no start falls behind. At 12 s, 229-250 of the hour's 432 runs ended.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta

from .planner import HourPlan

CONNECTION_BUDGET: dict[str, int] = {"proxy-cheap": 100}
DROPLET_CAP: dict[str, int] = {"proxy-cheap": 30}
COUNTRY_REQUESTS: dict[str, int] = {"proxy-cheap": 15_000}
PULSE_STEP_SECONDS: dict[str, int] = {"proxy-cheap": 30}
BROWSER_RESERVE = 10
MIN_RUN = 5


class BudgetError(ValueError):
    """The network's runs of the hour cannot share its budget at MIN_RUN each."""


def budgets() -> dict[str, int]:
    """The budgets in force (read at call time, so a test can set its own)."""
    return CONNECTION_BUDGET


def caps() -> dict[str, int]:
    """The most connections one droplet of a network holds (read at call time, like budgets)."""
    return DROPLET_CAP


def capped(network: str, paced: dict) -> dict:
    """The network's countries' pace ({country: pace}, daily.pace), each country's requests at
    most its COUNTRY_REQUESTS (unchanged for a network without them)."""
    most = COUNTRY_REQUESTS.get(network)
    if most is None:
        return paced
    return {c: p | {"requests": min(p.get("requests", most), most)} for c, p in paced.items()}


def pulse_step(networks: Iterable[str]) -> timedelta | None:
    """The slowest PULSE_STEP_SECONDS of `networks`, or None when none of them has one."""
    steps = [PULSE_STEP_SECONDS[n] for n in networks if n in PULSE_STEP_SECONDS]
    return timedelta(seconds=max(steps)) if steps else None


def share(network: str, runs: int) -> int | None:
    """The connections each of `runs` simultaneous runs of `network` may hold, at most its
    droplet cap, or None when the network has no budget (or nothing runs)."""
    total = budgets().get(network)
    if total is None or runs <= 0:
        return None
    each = (total - BROWSER_RESERVE) // runs
    if each < MIN_RUN:
        raise BudgetError(
            f"{network}: {runs} runs at once get {each} connections each, below {MIN_RUN}, "
            f"of a budget of {total} less {BROWSER_RESERVE} for its browser tests"
        )
    return min(each, caps().get(network, each))


def pulse(plan: HourPlan) -> dict[str, int]:
    """{network: connections per pulse droplet} of the hour's budgeted networks."""
    found = {}
    for network in plan.networks():
        each = share(network, sum(1 for cell in plan.cells if cell.network == network))
        if each is not None:
            found[network] = each
    return found


def daily(plan: HourPlan, template: dict[str, int]) -> dict[str, dict[str, int]]:
    """The hour's budgeted networks: {network: droplets, concurrency (each, at most the
    template's), total, budget}. Unlisted networks are left out."""
    found = {}
    for network in plan.networks():
        runs = sum(1 for cell in plan.cells if cell.network == network)
        each = share(network, runs)
        if each is not None:
            each = min(each, template[network])
            found[network] = {
                "droplets": runs,
                "concurrency": each,
                "total": runs * each,
                "budget": budgets()[network],
            }
    return found
