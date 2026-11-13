"""SIM-GEN-1: the DB-IP places of a generated country's IPs (SIMULATE.md, "Addresses and
recurrence"): the profile's top tuples by city, then by ASN inside a city (SIM-MODEL-3),
synthetic tail places, then unmatched."""

from __future__ import annotations

import bisect
import math
from itertools import accumulate

from synthetic import UNMATCHED, mix, skewed

from atlas.stats import LOCATION_FIELDS, NETWORK_FIELDS

# A Weyl step: any run of consecutive positions spreads evenly over [0, 1), so a lane's window
# takes each share of a distribution almost exactly, and an IP keeps its place from day to day.
PHI = (math.sqrt(5) - 1) / 2
ROOT3 = math.sqrt(3) - 1  # the second Weyl step: the tuple inside a city
CITY = ("country_code", "region", "city")
# The two-level draw: first the group (a city), then the tuple inside it, by its ASN's rank.
FIRST, SECOND = CITY, ("asn",)
TAIL_PLACES, TAIL_ASNS, PRIVATE_ASN = 200, 40, 4_200_000_000


def enrichment(place: dict) -> dict:
    """A profile place as the DB-IP enrichment of an IP (the fields the profile keeps)."""
    found = dict.fromkeys((*LOCATION_FIELDS, *NETWORK_FIELDS))
    found |= {name: place[name] for name in found if name in place}
    return found | {"asn_organization": place.get("asn_name"), "matched": True}


class Places:
    """A country's DB-IP places: the profile's top tuples, synthetic places for its tail (all of
    them for a country the profile did not request), and its unmatched share."""

    def __init__(self, country: str, measured: dict, salt: float):
        self.top, self.salt = measured.get("top") or [], salt
        unmatched = measured.get("unmatched_share")
        self.unmatched = UNMATCHED / 1000 if unmatched is None else unmatched
        self.cities = cities(self.top)
        shares = [sum(self.top[i]["share"] for i in city) for city in self.cities]
        self.cdf = list(accumulate([*shares, max(0.0, 1 - sum(shares) - self.unmatched)]))
        self.inside = [inside(self.top, city) for city in self.cities]
        self.entries = [enrichment(place) for place in self.top]
        for k in range(TAIL_PLACES):
            asn = k % TAIL_ASNS
            tail = {"country_code": country, "region": f"Synthetic Region {k % 20 + 1:02d}"}
            tail |= {"city": f"Synthetic City {k + 1:03d}", "asn": PRIVATE_ASN + asn}
            tail |= {"asn_name": f"Synthetic Network {asn + 1:02d}", "usage_type": "residential"}
            self.entries.append(enrichment(tail))

    def pick(self, position: int, n: int) -> int:
        """The place of an IP: a top tuple (< len(top)), a tail place, or -1 (unmatched). Each
        city is one slice of the first Weyl sequence, so a test takes each city's share almost
        exactly; the second picks the tuple inside it, by the ASNs' order."""
        c = bisect.bisect_right(self.cdf, (position * PHI + self.salt) % 1)
        if c < len(self.cities):
            inside, u = self.inside[c], (position * ROOT3 + self.salt) % 1
            return self.cities[c][min(bisect.bisect_right(inside, u), len(inside) - 1)]
        return len(self.top) + skewed(mix(n), TAIL_PLACES) if c == len(self.cities) else -1

    def enrichment(self, place: int) -> dict:
        return self.entries[place] if place >= 0 else {"matched": False}


def cities(top: list[dict]) -> list[list[int]]:
    """The top tuples' indexes by FIRST (a city; the cities by share), inside one by SECOND's
    share over the country (the most first), so an ASN takes about one slice in every city."""
    groups, seconds = {}, {}
    for i, place in enumerate(top):
        groups.setdefault(tuple(place.get(name) for name in FIRST), []).append(i)
        other = tuple(place.get(name) for name in SECOND)
        seconds[other] = seconds.get(other, 0) + place["share"]
    ranked = sorted(seconds, key=lambda k: (-seconds[k], repr(k)))
    rank = {k: r for r, k in enumerate(ranked)}
    found = [
        sorted(own, key=lambda i: (rank[tuple(top[i].get(name) for name in SECOND)], i))
        for own in groups.values()
    ]
    return sorted(found, key=lambda own: (-sum(top[i]["share"] for i in own), own[0]))


def inside(top: list[dict], city: list[int]) -> list[float]:
    """The cumulative shares of a city's tuples within the city."""
    found = list(accumulate(top[i]["share"] for i in city))
    return [n / found[-1] for n in found] if found[-1] else found
