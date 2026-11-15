"""An even 24-hour schedule for a network that holds few connections (PROXY-CHEAP-BUDGET-1).

Owner decision 2026-11-15: proxy-cheap supports few simultaneous connections, so its daily tests
cannot sit on one hour (199 of its 216 countries were on 13:00 UTC). `spread --network N`
rewrites that network's formula:

1. Each country's best hour comes from the other networks' own data: the hour most of them chose
   (best_hours.mode_hour over best_hours.best_hour), counting only their countries without a
   fallback reason (on their own country curve). Borrowed formulas are no donors. A country none
   of them has on its own curve has no best hour.
2. The countries are spread evenly: at most ceil(countries / 24) an hour, each as few hours from
   its best hour as possible, the least total circular distance (a min-cost transportation from
   best hours to hours with that capacity, solved by successive shortest paths). Countries
   without a best hour fill the hours left over, the quietest ones. Of the countries sharing a
   best hour, the ones most donors agree on keep it (ties: the country code). Deterministic.
3. The network takes the other networks' average hour curves (borrow.apply), so an estimate
   scales each test from its own hour. Its own 24-hour data is not broken, so each curve keeps its
   own day estimate: the daily factor becomes the old one x M / E, M the measured peak's unique
   IPs (the anchor) and E the borrowed peak's estimate (for the network curve, its measured day
   over E). `schedule` and every `test_hour` are the spread hours, `formula_version` goes up by
   one and `spread` records the rule, each best hour, the shift and, as `built`, the daily factors
   the formula was built with (the network's and its own-curve countries': what another formula
   borrowing from it took as a donor).

A formula that is already borrowed is refused: rebuild it first (the borrowed peak hour is no
measured anchor).
"""

from __future__ import annotations

import math

from atlas.formula import borrow
from atlas.formula.aggregate import FormulaError
from atlas.formula.best_hours import best_hour, mode_hour

HOURS = 24
NONE = -1  # the supply node of the countries without a best hour
RULE = (
    "best hour: the hour most other networks chose for the country on their own curve; then at "
    "most `capacity` countries an hour, the least total distance from the best hours; countries "
    "without one fill the hours left; donor agreement, then the country code, keeps a best hour"
)
DAILY = "own: the formula's day estimate is kept (its daily factor rescaled to the borrowed peak)"


def note(network: str) -> str:
    return f"borrowed: {borrow.SOURCE}; {network} is tested on an even 24-hour schedule"


def distance(best: int, hour: int) -> int:
    """Hours between two clock hours, around the clock."""
    gap = abs(best - hour) % HOURS
    return min(gap, HOURS - gap)


def best_hours(document: dict, donors: list[dict]) -> dict[str, tuple[int, int] | None]:
    """Each country: (the donors' mode hour, how many donors chose it), or None without one."""
    found: dict[str, tuple[int, int] | None] = {}
    for code in sorted(document["countries"]):
        choices = []
        for donor in donors:
            entry = donor["countries"].get(code)
            hour = best_hour(entry) if entry and not entry.get("fallback_reason") else None
            if hour is not None:
                choices.append((hour, entry["unique_ips"][hour]))
        if choices:
            hour = mode_hour(choices)
            found[code] = (hour, sum(1 for chosen, _ in choices if chosen == hour))
        else:
            found[code] = None
    return found


def _cost(best: int, hour: int) -> int:
    return 0 if best == NONE else distance(best, hour)


def _path(supply: dict[int, int], left: dict[int, int], flow: dict) -> list[int]:
    """The cheapest augmenting path [best, hour, best, hour, ...] in the residual graph (one
    exists while countries are left: transport checks the capacity). Bellman-Ford over the 24 +
    25 nodes, in a fixed order, so the result is deterministic."""
    inf = math.inf
    dist = {("b", b): (0 if supply[b] else inf) for b in supply}
    dist |= {("h", h): inf for h in range(HOURS)}
    back: dict[tuple, tuple] = {}
    for _ in range(len(dist)):
        changed = False
        for b in supply:
            for h in range(HOURS):
                step = dist[("b", b)] + _cost(b, h)
                if step < dist[("h", h)]:
                    dist[("h", h)], back[("h", h)], changed = step, ("b", b), True
                if flow.get((b, h)) and dist[("h", h)] - _cost(b, h) < dist[("b", b)]:
                    dist[("b", b)], back[("b", b)] = dist[("h", h)] - _cost(b, h), ("h", h)
                    changed = True
        if not changed:
            break
    ends = [h for h in range(HOURS) if left[h] and dist[("h", h)] < inf]
    node: tuple | None = ("h", min(ends, key=lambda h: (dist[("h", h)], h)))
    path = []
    while node is not None:
        path.append(node[1])
        node = back.get(node)
    return path[::-1]


def transport(supply: dict[int, int], capacity: int) -> dict[tuple[int, int], int]:
    """{(best hour, hour): countries}: every supplied country placed, at most `capacity` an hour,
    the least total distance (NONE costs 0 everywhere)."""
    if sum(supply.values()) > capacity * HOURS:
        raise FormulaError(f"{sum(supply.values())} countries do not fit {capacity} an hour")
    supply, left, flow = dict(supply), dict.fromkeys(range(HOURS), capacity), {}
    while any(supply.values()):
        path = _path(supply, left, flow)
        edges = list(zip(path[::2], path[1::2], strict=True))
        backward = list(zip(path[2::2], path[1::2], strict=False))
        amount = min([supply[path[0]], left[path[-1]], *(flow[edge] for edge in backward)])
        for edge in edges:
            flow[edge] = flow.get(edge, 0) + amount
        for edge in backward:
            flow[edge] -= amount
        supply[path[0]] -= amount
        left[path[-1]] -= amount
    return {edge: count for edge, count in sorted(flow.items()) if count}


def assign(best: dict[str, tuple[int, int] | None], capacity: int) -> dict[str, int]:
    """{country: hour}: the transportation's counts given to countries. Of one best hour, those
    most donors agree on take the hours nearest to it."""
    groups: dict[int, list[str]] = {}
    for code, chosen in best.items():
        groups.setdefault(NONE if chosen is None else chosen[0], []).append(code)
    flow = transport({b: len(codes) for b, codes in sorted(groups.items())}, capacity)
    schedule = {}
    for b, codes in sorted(groups.items()):
        ranked = sorted(codes, key=lambda code: (-(best[code] or (0, 0))[1], code))
        hours = sorted(
            (hour for (start, hour), count in flow.items() if start == b for _ in range(count)),
            key=lambda hour: (_cost(b, hour), hour),
        )
        schedule.update(zip(ranked, hours, strict=True))
    return dict(sorted(schedule.items()))


def spread(document: dict, donors: list[dict]) -> dict:
    """The document with borrowed curves and an even schedule (see the module docstring)."""
    if "borrowed" in document or "spread" in document:
        raise FormulaError(f"{document['network']}: already borrowed or spread; rebuild it first")
    best = best_hours(document, donors)
    capacity = math.ceil(len(best) / HOURS)
    schedule = assign(best, capacity)
    entries = [document["network_curve"], *document["countries"].values()]
    days = [(entry["peak_hour"], entry["daily_factor"]) for entry in entries]
    countries = document["countries"].items()
    own = {code: entry["daily_factor"] for code, entry in countries if entry["curve"] == "country"}
    built = {"daily_factor": document["network_curve"]["daily_factor"], "countries": own}
    borrow.apply(document, donors, note(document["network"]))
    for entry, (anchor, daily) in zip(entries, days, strict=True):
        peak = entry["estimated_unique_ips"][entry["peak_hour"]]
        if daily is not None and peak:
            entry["daily_factor"] = daily * entry["estimated_unique_ips"][anchor] / peak
    for code, hour in schedule.items():
        document["countries"][code]["test_hour"] = hour
    document["schedule"] = schedule
    document["formula_version"] += 1
    shifts = {code: distance(b[0], schedule[code]) for code, b in best.items() if b}
    document["spread"] = {
        "rule": RULE,
        "daily_factor": DAILY,
        "capacity": capacity,
        "best_hours": {code: b[0] if b else None for code, b in best.items()},
        "total_shift": sum(shifts.values()),
        "max_shift": max(shifts.values(), default=0),
        "built": built,
    }
    return document


def report(document: dict) -> list[str]:
    """The 24-hour table, the total shift and the countries moved most, as printed lines."""
    record, schedule = document["spread"], document["schedule"]
    head = f"{document['network']}: {len(schedule)} countries, at most {record['capacity']} an hour"
    lines = [head]
    for hour in range(HOURS):
        codes = [code for code, at in schedule.items() if at == hour]
        lines.append(f"  {hour:02d}  {len(codes):2d}  {' '.join(codes)}")
    best = record["best_hours"]
    moved = sorted(
        ((distance(best[c], h), c) for c, h in schedule.items() if best[c] is not None),
        key=lambda item: (-item[0], item[1]),
    )
    without = sorted(code for code, hour in best.items() if hour is None)
    lines.append(
        f"total shift {record['total_shift']} hours over {len(best) - len(without)} countries "
        f"with a best hour; at most {record['max_shift']}"
    )
    top = ", ".join(f"{c} {best[c]:02d}->{schedule[c]:02d}" for shift, c in moved[:10] if shift)
    lines.append(f"moved most: {top or 'none'}")
    lines.append(f"no best hour ({len(without)}): {' '.join(without)}")
    return lines
