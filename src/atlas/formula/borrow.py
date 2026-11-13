"""A borrowed hour curve: the average 24-hour shape of the other networks (temporary rule).

A network whose own campaign measured too few hours (byteful in version 2: IPs in one hour only)
is given, on request (`build --borrow <network>`), the average shape of every other network:

- a curve's shape is its hour factors divided by their mean, so every network weighs the same
  whatever its size; the borrowed shape of an hour is the mean of the donors' shapes that hour;
- a country takes the shape of the donors that have it on their own curve (`curve` "country")
  when there are at least MIN_COUNTRY_NETWORKS of them (its `curve` is then "country"), else the
  all-country shape (`curve` "network");
- the daily factor is the median of the donors' daily factors (each a whole day over its own
  peak hour, so here over the borrowed peak hour); the median, not the mean, so one network
  with an unusually large day does not raise every borrowed day;
- the shape S is anchored on the measured hour m (the measured peak; for byteful the one hour
  with IPs) with M unique IPs: every hour h is estimated as M x S[h] / S[m], hour m keeps M
  exactly (`estimated_unique_ips`, and `estimated_requests` likewise). The peak hour is where S
  is highest, the low hour where it is lowest, and the hour factors are S[h] / max(S). The
  measured counts (`unique_ips`, `requests`, `status`) stay as measured.

The document says so in `borrowed`, each country's `fallback_reason` repeats the note and every
hour's `hour_basis` is "borrowed", so the estimates scale requests by the hour factor. Rebuilding
without `--borrow` once the network's own data is complete replaces all of it.
"""

from __future__ import annotations

from statistics import fmean, median

from atlas.formula.aggregate import FormulaError

MIN_COUNTRY_NETWORKS = 3
SOURCE = "average of all networks"


def note(network: str) -> str:
    return f"borrowed: {SOURCE}, temporary until {network} data is fixed"


def _shape(entry: dict) -> list:
    """An entry's hour factors over their mean (None stays None)."""
    mean = fmean(factor for factor in entry["hour_factors"] if factor is not None)
    return [None if factor is None else factor / mean for factor in entry["hour_factors"]]


def average_shape(entries: list[dict]) -> tuple[list[float], float]:
    """The hour-by-hour mean of the entries' shapes and the median of their daily factors."""
    shapes = [_shape(entry) for entry in entries]
    hours = [
        fmean(shape[hour] for shape in shapes if shape[hour] is not None) for hour in range(24)
    ]
    return hours, median(entry["daily_factor"] for entry in entries)


def anchored(shape: tuple[list[float], float], entry: dict) -> dict:
    """The borrowed curve of `entry` (a network curve or a country) with the average `shape`.

    The anchor m is the entry's measured peak hour, with M unique IPs and R request rows. Every
    hour h is estimated as M x S[h] / S[m] (requests R x S[h] / S[m]), hour m exactly M and R.
    """
    hours, daily = shape
    anchor = entry["peak_hour"]
    peak = max(range(24), key=lambda hour: (hours[hour], -hour))

    def scaled(measured: int) -> list:
        return [measured if h == anchor else measured * hours[h] / hours[anchor] for h in range(24)]

    return {
        "hour_factors": [value / hours[peak] for value in hours],
        "daily_factor": daily,
        "peak_hour": peak,
        "low_hour": min(range(24), key=lambda hour: (hours[hour], hour)),
        "estimated_unique_ips": scaled(entry["unique_ips"][anchor]),
        "estimated_requests": scaled(entry["requests"][anchor]),
    }


def _country(code: str, entry: dict, donors: list[dict], all_shape: tuple, reason: str) -> None:
    entries = [donor["countries"].get(code, {}) for donor in donors]
    same = [other for other in entries if other.get("curve") == "country"]
    own = len(same) >= MIN_COUNTRY_NETWORKS
    entry.update(anchored(average_shape(same) if own else all_shape, entry))
    entry.update(
        curve="country" if own else "network",
        hour_basis=["borrowed"] * 24,
        test_hour=entry["peak_hour"],
        fallback_reason=f"{reason} ({len(same)} networks have {code} on their own curve)",
    )


def apply(document: dict, donors: list[dict]) -> dict:
    """The document with the donors' average curve in place of its own (see the docstring)."""
    if not donors:
        raise FormulaError(f"{document['network']}: no other network to borrow a curve from")
    network = document["network_curve"]
    all_shape = average_shape([donor["network_curve"] for donor in donors])
    network.update(anchored(all_shape, network))
    reason = note(document["network"])
    for code, entry in document["countries"].items():
        _country(code, entry, donors, all_shape, reason)
        document["schedule"][code] = entry["test_hour"]
    document["borrowed"] = {
        "source": SOURCE,
        "note": reason,
        "networks": sorted(donor["network"] for donor in donors),
        "min_country_networks": MIN_COUNTRY_NETWORKS,
    }
    return document
