"""PROXY-CHEAP-BUDGET-1: a network's budget of simultaneous connections in one hour, shared by
its daily runs, pulses and browser tests across all regions (owner decision 2026-11-15).

proxy-cheap holds few connections at once (live sweep 2026-11-15 05:03-05:12Z: 50-100 at once
95-99% answered, 250 about 84% with 503s and slow answers, 500 about 39% with HTTP 429; about
30-40 successful requests a second at most). Everything that tests a network listed in
CONNECTION_BUDGET within one hour shares that many connections; BROWSER_RESERVE of them stay
free for its browser tests. The planners split the rest by what runs at the same time:

- the daily phase (daily.runner): the hour's n droplets of the network, one per region with a
  country due (planner.plan_hour), each run at floor((budget - reserve) / n), at most the
  template's concurrency. A worker runs its countries one after another, so the hour holds at
  most n times that.
- the pulse phase: each of the hour's pulse droplets of the network holds at most
  floor((budget - reserve) / droplets) connections at once.

A share below MIN_RUN would not fit the budget and is refused (`BudgetError`): with 8 regions
the smallest share is 30. A network not listed keeps its settings, byte for byte. A budget
changes in code (a pull request and a deploy); docs/campaign.html has the runbook.
"""

from __future__ import annotations

from .planner import HourPlan

CONNECTION_BUDGET: dict[str, int] = {"proxy-cheap": 250}
BROWSER_RESERVE = 10
MIN_RUN = 5


class BudgetError(ValueError):
    """The network's runs of the hour cannot share its budget at MIN_RUN each."""


def budgets() -> dict[str, int]:
    """The budgets in force (read at call time, so a test can set its own)."""
    return CONNECTION_BUDGET


def share(network: str, runs: int) -> int | None:
    """The connections each of `runs` simultaneous runs of `network` may hold, or None when the
    network has no budget (or nothing runs)."""
    total = budgets().get(network)
    if total is None or runs <= 0:
        return None
    each = (total - BROWSER_RESERVE) // runs
    if each < MIN_RUN:
        raise BudgetError(
            f"{network}: {runs} runs at once get {each} connections each, below {MIN_RUN}, "
            f"of a budget of {total} less {BROWSER_RESERVE} for its browser tests"
        )
    return each


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
