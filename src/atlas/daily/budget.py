"""PROXY-CHEAP-BUDGET-1/2: a network's budget of simultaneous connections in one hour, shared by
its daily runs, pulses and browser tests across all regions (owner decisions 2026-11-15).

proxy-cheap holds few connections at once (live sweep 2026-11-15 05:03-05:12Z: 50-100 at once
95-99% answered, 250 about 84% with 503s and slow answers, 500 about 39% with HTTP 429; about
30-40 successful requests a second at most). Its first hour on a budget of 250 (16:00Z, 3 daily
droplets x 80) answered 19%, mostly HTTP 503, so its budget was 100 (40 since PC-BUDGET-1, below)
and each droplet holds at most 30 (PROXY-CHEAP-BUDGET-2). Everything that tests a network listed
in CONNECTION_BUDGET within one hour shares that many connections; BROWSER_RESERVE of them stay
free for its browser tests. The planners split the rest by what runs at the same time, each
droplet at most the network's DROPLET_CAP:

- the daily phase (daily.runner): the hour's n droplets of the network, one per region with a
  country due (planner.plan_hour), each run at floor((budget - reserve) / n), at most the cap
  and the template's concurrency. A worker runs its countries one after another, so the hour
  holds at most n times that.
- the pulse phase (`pulse`, campaign.pulse_ondemand): each of the hour's pulse droplets of the
  network holds at most floor((budget - reserve) / droplets) connections at once, at most the
  cap (`atlas node --max-connections`, node_pool.py), after the daily runs
  (campaign.pulse_late).

A daily share below MIN_RUN would not fit the budget and is refused (`BudgetError`); a pulse
droplet's floor is node_pool.RUNS_AT_ONCE, one connection for each run it holds at once
(PC-BUDGET-1, below). Each daily country test of a budgeted network makes at most its
COUNTRY_REQUESTS requests (`capped`, DAILY-BUDGET-STOP-1), so a big country never runs half an
hour at its small share (proxy-cheap's Brazil ran about 13 requests a second on 22 connections:
15,000 is about 20 minutes). A network not listed keeps its settings, byte for byte. A budget
changes in code (a pull request and a deploy); docs/campaign.html has the runbook.

PC-BUDGET-1: proxy-cheap's account of 18 Nov 2026 answers about 41 to 50 connections at once (a
CONNECT probe: 50 of 50 answered, 41 of 100 and the rest HTTP 429), so its budget is 40, the
measured limit of the account with a margin, and BROWSER_RESERVE is 8: 32 are left, a daily hour
of n regions runs 32 // n per droplet (its busiest hours have 6 regions: 5 each) and its 8 pulse
droplets get 4 each, 2 per run (with 10 kept, 3 each: one connection per run, and a far region's
run of about 5 minutes would outlast pulse_late.PACED_ROOM). 4 is under MIN_RUN, so a pulse
droplet's floor is RUNS_AT_ONCE instead.

PULSE-STEP-1: a budgeted network's pulses are the same 100 requests as every network's (owner
rule of 2026-11-16, a fair ranking), every country every hour, paced to its share: its late slot
(campaign.pulse_late) starts at most one country every PULSE_STEP_SECONDS on each droplet
instead of pulse.step_seconds (12), the two targets half a step apart. A run holds its droplet's
share over node_pool.RUNS_AT_ONCE (proxy-cheap: 11 // 2 = 5 on its budget of 100, 4 // 2 = 2
since PC-BUDGET-1); 100 requests at 5 took about 29 s (15 Nov 2026). At 12 s, 229-250 of the
hour's 432 runs ended. PULSE-STEP-2: run times depend on the droplet's region (16 Nov: fra1
about 22 s, syd1 63 s), so the step is a floor and each droplet gets its next run only when one
of its RUNS_AT_ONCE slots is free (pulse_late.Puller): no run waits for connections, and none
falls behind its start.

BROWSER-BUDGET-1: the browser tests keep to that reserve. A budgeted network's browser tries on
its host (one host per network, browser/settings.py) run one at a time, and their gateway
connections are at most `browser_connections` at once (browser/gate.py): before it, a heavy
page's tunnels on 2 browsers went far over 10, and proxy-cheap's pulses during its LinkedIn run
answered 2-12% (17 Nov 2026).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta

from ..node_pool import RUNS_AT_ONCE
from .planner import HourPlan

CONNECTION_BUDGET: dict[str, int] = {"proxy-cheap": 40}
DROPLET_CAP: dict[str, int] = {"proxy-cheap": 30}
COUNTRY_REQUESTS: dict[str, int] = {"proxy-cheap": 15_000}
PULSE_STEP_SECONDS: dict[str, int] = {"proxy-cheap": 30}
BROWSER_RESERVE = 8
MIN_RUN = 5


class BudgetError(ValueError):
    """The network's runs of the hour cannot share its budget at their floor each."""


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


def share(network: str, runs: int, least: int = MIN_RUN) -> int | None:
    """The connections each of `runs` simultaneous runs of `network` may hold, at most its
    droplet cap, or None when the network has no budget (or nothing runs); refused below
    `least`."""
    total = budgets().get(network)
    if total is None or runs <= 0:
        return None
    each = (total - BROWSER_RESERVE) // runs
    if each < least:
        raise BudgetError(
            f"{network}: {runs} runs at once get {each} connections each, below {least}, "
            f"of a budget of {total} less {BROWSER_RESERVE} for its browser tests"
        )
    return min(each, caps().get(network, each))


def browser_connections(network: str) -> int | None:
    """The gateway connections the browser tries of `network` (its provider, such as
    "proxy-cheap") hold at once: its BROWSER_RESERVE, or None when it has no budget."""
    return BROWSER_RESERVE if network in budgets() else None


def pulse(plan: HourPlan) -> dict[str, int]:
    """{network: connections per pulse droplet} of the hour's budgeted networks, each at least
    one for every run the droplet holds at once (RUNS_AT_ONCE)."""
    found = {}
    for network in plan.networks():
        runs = sum(1 for cell in plan.cells if cell.network == network)
        each = share(network, runs, least=RUNS_AT_ONCE)
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
