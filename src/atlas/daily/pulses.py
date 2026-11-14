"""PULSE-ONDEMAND-1: the hour's on-demand pulse droplets, one per network and region.

Every hour each network gets one temporary droplet per runner region that serves any of its
countries (the countries of its formula, `formulas/<network>.json`, grouped by
configs/regions.yaml as the daily planner does). The droplet runs the `generic_http` and
`generic_https` pulses of all those countries (100 requests each at concurrency 25, the targets'
pulse settings) and is deleted once they are done; there is no per-droplet country limit.

The plan is a dry run: it writes nothing and calls no cloud. `check_cap` is the droplet guard:
an hour that needs more than the cap (DEFAULT_CAP, configurable) creates none. The account's
limit is 500 droplets; the daily test needs at most 74 in an hour and 18 networks x 8 regions
is 144.
"""

from __future__ import annotations

from dataclasses import replace

from ..regions import RegionMap
from .planner import HourPlan, plan_hour

DEFAULT_CAP = 144  # the most pulse droplets an hour may create (18 networks x 8 regions)
ACCOUNT_LIMIT = 500  # DigitalOcean's droplet limit on the account


class CapError(ValueError):
    """The hour needs more pulse droplets than the cap allows; nothing is created."""


def pulse_hour(
    schedules: dict[str, dict[str, int]],
    hour: int,
    *,
    networks: tuple[str, ...] | None = None,
    regions: RegionMap | None = None,
) -> HourPlan:
    """The hour's cells: every country of each network's formula is due, whatever its hour."""
    every = {network: dict.fromkeys(schedule, 0) for network, schedule in schedules.items()}
    return replace(plan_hour(every, 0, networks=networks, regions=regions), hour=hour)


def check_cap(plan: HourPlan, cap: int = DEFAULT_CAP, running: int = 0) -> None:
    """Refuse an hour whose droplets, with `running` pulse droplets still up, exceed `cap`."""
    if type(cap) is not int or not 0 < cap <= ACCOUNT_LIMIT:
        raise CapError(f"the pulse droplet cap must be 1..{ACCOUNT_LIMIT}")
    wanted = len(plan.cells) + running
    if wanted > cap:
        raise CapError(
            f"the hour needs {len(plan.cells)} pulse droplets and {running} are still up: "
            f"{wanted} is over the cap of {cap}"
        )
