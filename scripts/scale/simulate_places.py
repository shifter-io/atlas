"""SIM-GEN-1: the DB-IP places of a generated country's IPs (SIMULATE.md, "Addresses and
recurrence"): the profile's top tuples by share, synthetic tail places, then unmatched."""

from __future__ import annotations

import bisect
import math
from itertools import accumulate

from synthetic import UNMATCHED, mix, skewed

from proxy_index.stats import LOCATION_FIELDS, NETWORK_FIELDS

# A Weyl step: any run of consecutive positions spreads evenly over [0, 1), so a lane's window
# takes each share of a distribution almost exactly, and an IP keeps its place from day to day.
PHI = (math.sqrt(5) - 1) / 2
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
        shares = [place["share"] for place in self.top]
        self.cdf = list(accumulate([*shares, max(0.0, 1 - sum(shares) - self.unmatched)]))
        self.entries = [enrichment(place) for place in self.top]
        for k in range(TAIL_PLACES):
            asn = k % TAIL_ASNS
            tail = {"country_code": country, "region": f"Synthetic Region {k % 20 + 1:02d}"}
            tail |= {"city": f"Synthetic City {k + 1:03d}", "asn": PRIVATE_ASN + asn}
            tail |= {"asn_name": f"Synthetic Network {asn + 1:02d}", "usage_type": "residential"}
            self.entries.append(enrichment(tail))

    def pick(self, position: int, n: int) -> int:
        """The place of an IP: a top tuple (< len(top)), a tail place, or -1 (unmatched)."""
        i = bisect.bisect_right(self.cdf, (position * PHI + self.salt) % 1)
        if i < len(self.top):
            return i
        return len(self.top) + skewed(mix(n), TAIL_PLACES) if i == len(self.top) else -1

    def enrichment(self, place: int) -> dict:
        return self.entries[place] if place >= 0 else {"matched": False}
