"""SIM-OVERLAP-1: the IPs two networks share in the generated days (SIMULATE.md, "Shared IPs").

Each pair of networks of the profile's `overlap`, in each country both schedule with a share on
both sides, has a shared lane: its own block of IP indexes after the networks' lanes (synthetic
addresses only). Its window of a day is the pair's shared IPs that day, and both networks' tests
take all of it, so the same addresses appear in both, as a device sold through two pools does.
A test takes the rest of its size from its own lane. A shared window slides from day to day as a
lane's does, at the lower recurrence of the two networks; the own window recurs what the
network's recurrence still needs, so the test's recurrence holds.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass

from simulate_browser import dig
from synthetic import JITTER


@dataclass(frozen=True)
class Pool:
    """The shared lane of two networks in one country: the share of each one's daily IPs drawn
    from it (scaled down where a lane's shares add up to more than 1), the most it may take of
    each (its part of the lane's shares, so a lane's pools never take more than the lane), its
    recurrence and its first IP index."""

    networks: tuple[str, str]
    country: str
    shares: tuple[float, float]
    room: tuple[float, float]
    reuse: float
    offset: int


def source(profile: dict, pair: dict, country: str) -> dict | None:
    """The profile's figures (`shared`, `shares`) a country of the pair takes: the country's
    when measured; none when both networks requested it and shared no IP there; else the
    network's."""
    if country in pair.get("countries", {}):
        return pair["countries"][country]
    names = pair["networks"]
    return None if all(country in dig(profile, "networks", n, "places") for n in names) else pair


def pools(profile: dict, lanes: dict, offset: int, days: int) -> dict[tuple[str, str], list]:
    """(network, country) -> [(pool, the network's side: 0 or 1)], the blocks from `offset`."""
    found = []
    for pair in dig(profile, "overlap").get("pairs") or []:
        a, b = pair["networks"]
        for country in sorted({c for n, c in lanes if n == a} & {c for n, c in lanes if n == b}):
            entry = source(profile, pair, country) or {"shares": {}}
            shares = tuple(entry["shares"].get(n) or 0.0 for n in (a, b))
            if all(shares):
                found.append(((a, b), country, shares))
    totals = Counter()
    for names, country, shares in found:
        totals.update({(n, country): s for n, s in zip(names, shares, strict=True)})
    by_lane: dict[tuple[str, str], list] = {}
    for names, country, shares in found:
        own, parts = [lanes[n, country] for n in names], zip(names, shares, strict=True)
        room = tuple(s / totals[n, country] for n, s in parts)
        scaled = tuple(min(pair) for pair in zip(shares, room, strict=True))  # s / max(1, total)
        pool = Pool(names, country, scaled, room, min(lane.reuse for lane in own), offset)
        offset += math.ceil(min(lane.ips for lane in own) * (1 + JITTER)) * days
        for side, name in enumerate(names):
            by_lane.setdefault((name, country), []).append((pool, side))
    return by_lane


def slides(days: list[tuple[int, int]]) -> list[tuple[int, int, int]]:
    """Per day (size, wanted recurring) of a block, its window: (first position, size, positions
    also in the day before's window). A window starts its recurring positions before the end of
    the day before's."""
    found, end, size = [], 0, 0
    for wanted_size, wanted in days:
        recurring = max(0, min(wanted, size, wanted_size))
        start, size = end - recurring, max(0, wanted_size)
        end = start + size
        found.append((start, size, recurring))
    return found


def taken(model, pool: Pool, day: int) -> int:
    """The pool's shared IPs on a day: the geometric mean of the two networks' shares of their
    tests (each side within the profile's share by half the two sizes' mismatch), within the
    room of each."""
    sizes = [model.size(model.lanes[name, pool.country], day) for name in pool.networks]
    wanted = math.sqrt(math.prod(s * n for s, n in zip(pool.shares, sizes, strict=True)))
    return min(round(wanted), *(math.floor(r * n) for r, n in zip(pool.room, sizes, strict=True)))


def blocks(model, lane, day: int) -> tuple[tuple[int, int, int, int], ...]:
    """A test's (first IP index, first window position, IPs, IPs also in the day before's test)
    per block: the lane's own, then its pools'."""
    days = range(1, day + 1)
    own = [pool for pool, _ in model.pools.get((lane.network, lane.country), [])]
    pooled = [
        slides([(k, round(pool.reuse * k)) for k in (taken(model, pool, d) for d in days)])
        for pool in own
    ]
    mine = []
    for i, d in enumerate(days):
        size = model.size(lane, d)
        shared, recurring = sum(w[i][1] for w in pooled), sum(w[i][2] for w in pooled)
        mine.append((size - shared, round(lane.reuse * size) - recurring))
    found = [(lane.offset, *slides(mine)[-1])]
    return tuple(found + [(pool.offset, *w[-1]) for pool, w in zip(own, pooled, strict=True)])
