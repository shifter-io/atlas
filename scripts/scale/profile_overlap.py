"""SIM-OVERLAP-1: the IPs two networks share in the real 24 hours (SIMULATE.md, "overlap"), from
each network's distinct IP hashes per requested country (profile_hours.Network.days). Counts and
shares only, never an IP."""

from __future__ import annotations

import heapq
from array import array
from collections import Counter
from itertools import combinations

from profile_hours import share

BASIS = (
    "distinct IPs of the 24 hours: over the network, and per country both networks requested "
    "(the IPs that served that country's requests)"
)


def _tagged(values: array, network: str, code: str):
    for value in values:
        yield value, network, code


def overlap(days: dict[str, dict[str, array]]) -> dict:
    """The pairs of networks with a shared IP. `days`: network -> country -> its sorted,
    distinct hashes. Each pair has `shared` and `shares` (of each network's distinct IPs) over
    the network, and per country with a shared IP the same over the two countries' IPs."""
    sizes, pairs, countries = Counter(), Counter(), Counter()
    tagged = (_tagged(v, network, code) for network, own in days.items() for code, v in own.items())
    last, group = None, []
    for value, network, code in heapq.merge(*tagged):
        if value != last:
            _count(group, sizes, pairs, countries)
            last, group = value, []
        group.append((network, code))
    _count(group, sizes, pairs, countries)
    by_pair: dict[tuple[str, str], dict] = {}
    for (a, b, code), n in sorted(countries.items()):
        shares = {x: share(n, len(days[x][code])) for x in (a, b)}
        by_pair.setdefault((a, b), {})[code] = {"shared": n, "shares": shares}
    found = []
    for (a, b), shared in sorted(pairs.items()):
        entry = {"networks": [a, b], "shared": shared, "countries": by_pair.get((a, b), {})}
        found.append(entry | {"shares": {a: share(shared, sizes[a]), b: share(shared, sizes[b])}})
    return {"basis": BASIS, "pairs": found}


def _count(group: list[tuple[str, str]], sizes: Counter, pairs: Counter, countries: Counter):
    """Count one IP: its (network, country) tags."""
    if len(group) == 1:  # most IPs: one network, one country
        sizes[group[0][0]] += 1
        return
    networks = sorted({network for network, _ in group})
    sizes.update(networks)
    pairs.update(combinations(networks, 2))
    by_country: dict[str, list[str]] = {}
    for network, code in sorted(group):
        by_country.setdefault(code, []).append(network)
    for code, own in by_country.items():
        countries.update((a, b, code) for a, b in combinations(own, 2))
