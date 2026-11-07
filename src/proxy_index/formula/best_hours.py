"""The best hour of each country of a network: the UTC hour with the most unique live IPs.

An IP is live in an hour when it answered at least once in it. Over all 24 hours of a country's
own `unique_ips` in its formula, the best hour has the most unique IPs; ties go to more request
rows (`requests`), then to the earlier hour. A country with no IP in any hour has no best hour
and is listed under `no_ips`.

A borrowed formula (`borrowed` in the document, byteful in version 2) has IPs in too few hours for
its own curve to give a best hour. Each of its countries takes the hour most of the other
networks chose for the same country (the mode; ties go to the larger sum of the choosing networks'
unique IPs at that hour, then to the earlier hour), with `basis` "borrowed" and the number of
donor networks. A country no other network has keeps the formula's borrowed `peak_hour`. Its
`unique_ips` is always the network's own measured count at the chosen hour.
"""

from __future__ import annotations

from collections import defaultdict

SCHEMA = "proxy-index-best-hours"
SCHEMA_VERSION = 1
RULE = (
    "the UTC clock hour with the most unique live IPs (an IP answered at least once in it); "
    "ties: more request rows, then the earlier hour; a borrowed formula takes the hour most "
    "other networks chose for the country (ties: more of their unique IPs, then the earlier hour)"
)


def best_hour(entry: dict) -> int | None:
    """The hour with the most unique IPs (ties: more requests, then earlier); None without IPs."""
    ips, requests = entry["unique_ips"], entry["requests"]
    if not any(ips):
        return None
    return max(range(24), key=lambda hour: (ips[hour], requests[hour], -hour))


def own_hours(document: dict) -> dict[str, tuple[int, int]]:
    """Each country with IPs: (best hour, unique IPs at that hour)."""
    hours = {}
    for code, entry in document["countries"].items():
        hour = best_hour(entry)
        if hour is not None:
            hours[code] = (hour, entry["unique_ips"][hour])
    return hours


def mode_hour(choices: list[tuple[int, int]]) -> int:
    """The hour most choices name (ties: larger sum of their IPs, then the earlier hour)."""
    votes: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for hour, ips in choices:
        votes[hour][0] += 1
        votes[hour][1] += ips
    return max(votes, key=lambda hour: (votes[hour][0], votes[hour][1], -hour))


def _entry(entry: dict, hour: int, basis: str, donors: int) -> dict:
    return {"hour": hour, "unique_ips": entry["unique_ips"][hour], "basis": basis, "donors": donors}


def best_hours(document: dict, donors: list[dict[str, tuple[int, int]]]) -> dict:
    """The best-hours document of one formula; `donors` are the other networks' own_hours."""
    borrowed = "borrowed" in document
    countries, no_ips = {}, []
    for code, entry in sorted(document["countries"].items()):
        if best_hour(entry) is None:
            no_ips.append(code)
        elif borrowed:
            choices = [donor[code] for donor in donors if code in donor]
            hour = mode_hour(choices) if choices else entry["peak_hour"]
            countries[code] = _entry(entry, hour, "borrowed", len(choices))
        else:
            countries[code] = _entry(entry, best_hour(entry), "own", 0)
    hours: dict[str, list[str]] = {f"{hour:02d}": [] for hour in range(24)}
    for code, chosen in countries.items():
        hours[f"{chosen['hour']:02d}"].append(code)
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "network": document["network"],
        "rule": RULE,
        "hours": hours,
        "countries": countries,
        "no_ips": no_ips,
    }


def build_all(documents: list[dict]) -> list[dict]:
    """The best-hours document of every formula; borrowed formulas borrow from the others."""
    donors = [own_hours(document) for document in documents if "borrowed" not in document]
    return [best_hours(document, donors) for document in documents]
