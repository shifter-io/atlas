"""The public hour formula of one network, built from its verified campaign counts.

A curve is 24 UTC clock hours of unique IPs. Its peak hour has the most unique IPs (tie: more
request rows, then the earlier hour) and its low hour the fewest (tie: fewer request rows, then
the earlier hour). An hour in which the network's campaign saw no exit IP (status missing or
requests_no_ips) is no data: neither peak nor low, its factor is null and
network_curve.no_data_reasons says why. Then

- hour factor = the hour's unique IPs / the peak hour's unique IPs (the peak is exactly 1.0);
- daily factor = whole-day unique IPs (union over the hours) / the peak hour's unique IPs.

Factors are stored at full float precision, so round(factor * peak IPs) gives back each measured
hour and the whole day exactly. A country whose peak hour has fewer than min_peak_ips unique IPs,
or that has IPs in fewer than min_hours hours (capped at the network's own hours with IPs), uses
the network's all-country curve instead, marked curve "network" with the reason. Each country's
test hour (the daily test schedule) is the peak hour of the curve it uses.

A country on its own curve can still lack one hour: no request row at all (`network_missing`) or
fewer request rows than min_sample_share x the median of its covered hours (`network_low_sample`),
where a covered hour is one in which the network itself reported IPs. Such an hour is not a
measurement of the pool, so it is neither peak nor low and its factor comes from the network
curve, scaled to the country's level: network IPs that hour x (the country's IPs / the network's
IPs over its measured hours) / the country's peak IPs, at most 1.0. An hour under
low_sample_share x the median keeps its own count but is flagged `country_low_sample`.
`hour_basis` records this per hour.
"""

from __future__ import annotations

import json
import re
import statistics

from atlas import legacy_names
from atlas.formula.aggregate import HOURS, FormulaError

NO_DATA = {
    "missing": "no data: no request was made in this hour",
    "requests_no_ips": "no data: requests were answered in this hour but no exit IP was reported",
}
SCHEMA = "atlas-hour-formula"
SCHEMAS = (SCHEMA, legacy_names.HOUR_FORMULA)  # a pre-rename formula file reads too
SCHEMA_VERSION = 1
DEFAULT_MIN_PEAK_IPS = 100
DEFAULT_MIN_HOURS = 20
# Shares of the median request rows of a country's covered hours (see the module docstring).
DEFAULT_MIN_SAMPLE_SHARE = 0.25
DEFAULT_LOW_SAMPLE_SHARE = 0.5
FALLBACK_HOURS = ("network_missing", "network_low_sample")
LATENCY_NOTE = (
    "not derivable: the campaign hour files hold one record per exit IP with its request "
    "count but no per-request latency; the daily test measures latency"
)
_SCALAR_LIST = re.compile(r"\[\s+([^\[\]{}]*?)\s+\]")


def curve(unique_ips: list[int], requests: list[int], measured: list[bool], day: int) -> dict:
    """Peak, low and factors of one 24-hour curve; unmeasured hours get a null factor."""
    hours = [hour for hour, known in enumerate(measured) if known]
    if not hours:
        raise FormulaError("no measured hour")
    peak = max(hours, key=lambda hour: (unique_ips[hour], requests[hour], -hour))
    low = min(hours, key=lambda hour: (unique_ips[hour], requests[hour], hour))
    peak_ips = unique_ips[peak]
    if peak_ips <= 0:
        raise FormulaError("the peak hour has no unique IP")
    return {
        "peak_hour": peak,
        "low_hour": low,
        "hour_factors": [
            unique_ips[hour] / peak_ips if known else None for hour, known in enumerate(measured)
        ],
        "daily_factor": day / peak_ips,
    }


def _network_curve(counts: dict) -> dict:
    hours = counts["hours"]
    status = [hour["status"] for hour in hours]
    unique_ips = [hour["unique_ips"] for hour in hours]
    requests = [hour["requests"] for hour in hours]
    measured = [value == "present" for value in status]
    return {
        "status": status,
        "no_data_reasons": [NO_DATA.get(value) for value in status],
        "unique_ips": unique_ips,
        "requests": requests,
        "attempted_requests": [hour["attempted_requests"] for hour in hours],
        "day_unique_ips": counts["day_unique_ips"],
        "hours_with_ips": sum(1 for value in unique_ips if value),
        **curve(unique_ips, requests, measured, counts["day_unique_ips"]),
    }


def _fallback_reason(own: dict, peak_ips: int, min_peak_ips: int, min_hours: int) -> str | None:
    if peak_ips < min_peak_ips:
        return f"peak hour has {peak_ips} unique IPs (< {min_peak_ips})"
    if own["hours_with_ips"] < min_hours:
        return f"IPs in {own['hours_with_ips']} hours (< {min_hours})"
    return None


def _sample_basis(rows: int, median: float, shares: tuple[float, float]) -> str:
    if not rows:
        return "network_missing"
    if rows < shares[0] * median:
        return "network_low_sample"
    return "country_low_sample" if rows < shares[1] * median else "country"


def hour_basis(requests: list[int], status: list[str], shares: tuple[float, float]) -> list:
    """Per hour, what a country's own-curve factor rests on; None where the network has no IP."""
    covered = [requests[hour] for hour, value in enumerate(status) if value == "present"]
    median = statistics.median(covered) if covered else 0
    return [
        _sample_basis(requests[hour], median, shares) if value == "present" else None
        for hour, value in enumerate(status)
    ]


def _fill_from_network(factors: list, basis: list, own: list[int], network: list[int]) -> list:
    """Replace the fallback hours' factors by the network curve at the country's level, at most
    1.0 so that the measured peak stays the largest factor."""
    kept = [hour for hour, value in enumerate(basis) if value and value not in FALLBACK_HOURS]
    level = sum(own[hour] for hour in kept) / sum(network[hour] for hour in kept)
    peak = max(own[hour] for hour in kept)
    return [
        min(network[hour] * level / peak, 1.0) if value in FALLBACK_HOURS else factor
        for hour, (factor, value) in enumerate(zip(factors, basis, strict=True))
    ]


def _country(own: dict, network: dict, rules: dict) -> dict:
    status = network["status"]
    basis = hour_basis(
        own["requests"], status, (rules["min_sample_share"], rules["low_sample_share"])
    )
    measured = [
        value == "present" and basis[hour] not in FALLBACK_HOURS
        for hour, value in enumerate(status)
    ]
    entry = {
        "unique_ips": own["unique_ips"],
        "requests": own["requests"],
        "day_unique_ips": own["day_unique_ips"],
        "hours_with_ips": sum(1 for value in own["unique_ips"] if value),
    }
    measured_curve = curve(own["unique_ips"], own["requests"], measured, own["day_unique_ips"])
    entry["peak_hour"] = measured_curve["peak_hour"]
    entry["low_hour"] = measured_curve["low_hour"]
    peak_ips = own["unique_ips"][measured_curve["peak_hour"]]
    floor = min(rules["min_hours_with_ips"], network["hours_with_ips"])
    reason = _fallback_reason(entry, peak_ips, rules["min_peak_unique_ips"], floor)
    if reason:
        used = network
        basis = ["network" if value == "present" else None for value in status]
    else:
        used = measured_curve | {
            "hour_factors": _fill_from_network(
                measured_curve["hour_factors"], basis, own["unique_ips"], network["unique_ips"]
            )
        }
    entry.update(
        curve="network" if reason else "country",
        fallback_reason=reason,
        hour_factors=used["hour_factors"],
        hour_basis=basis,
        daily_factor=used["daily_factor"],
        test_hour=used["peak_hour"],
    )
    return entry


def build_formula(
    counts: dict,
    version: int,
    min_peak_ips: int,
    min_hours: int,
    sample_shares: tuple[float, float] = (DEFAULT_MIN_SAMPLE_SHARE, DEFAULT_LOW_SAMPLE_SHARE),
) -> dict:
    """The public formula document of one network's counts (see formulas/README.md)."""
    if len(counts["hours"]) != len(HOURS):
        raise FormulaError(f"{counts['network']}: counts need 24 hours")
    network = _network_curve(counts)
    rules = {
        "hour": "UTC clock hour of the round's scheduled start",
        "country": "requested country (the test's country parameter)",
        "requests": "request rows: successful requests with an exit IP",
        "min_peak_unique_ips": min_peak_ips,
        "min_hours_with_ips": min_hours,
        "hour_sample": "a country hour's request rows / the median of its covered hours",
        "min_sample_share": sample_shares[0],
        "low_sample_share": sample_shares[1],
    }
    countries = {
        code: _country(own, network, rules) for code, own in sorted(counts["countries"].items())
    }
    hour_runs = [hour["runs"] for hour in counts["hours"]]
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "formula_version": version,
        "network": counts["network"],
        "product": counts["product"],
        "source": {
            "campaign_runs": sorted({run for runs in hour_runs for run in runs}),
            "hour_runs": hour_runs,
            "enrichment_manifest_sha256": counts["manifest_sha256"],
            "dbip_release_sha256": counts["dbip_release_sha256"],
        },
        "rules": rules,
        "latency": {"available": False, "note": LATENCY_NOTE},
        "network_curve": network,
        "countries": countries,
        "schedule": {code: entry["test_hour"] for code, entry in countries.items()},
    }


def dumps(document: dict) -> str:
    """Indented JSON with every list of numbers or strings kept on one line."""
    text = json.dumps(document, indent=2)
    return _SCALAR_LIST.sub(lambda match: "[" + re.sub(r",\s+", ", ", match[1]) + "]", text) + "\n"


def _row(document: dict) -> str:
    network = document["network_curve"]
    countries = document["countries"]
    fallback = sum(1 for entry in countries.values() if entry["curve"] == "network")
    no_ip = sum(1 for status in network["status"] if status == "requests_no_ips")
    missing = sum(1 for status in network["status"] if status == "missing")
    test_hours = len(set(document["schedule"].values()))
    name = document["network"] + (" (borrowed curve)" if "borrowed" in document else "")
    hour_ips = network.get("estimated_unique_ips", network["unique_ips"])
    peak_ips = round(hour_ips[network["peak_hour"]])
    return (
        f"| {name} | {document['formula_version']} | {network['hours_with_ips']} "
        f"| {no_ip} | {missing} | {network['peak_hour']:02d} | {network['low_hour']:02d} "
        f"| {peak_ips:,} | {network['day_unique_ips']:,} "
        f"| {network['daily_factor']:.3f} | {len(countries)} | {fallback} | {test_hours} "
        f"| {', '.join(document['source']['campaign_runs'])} |"
    )


def summary_markdown(documents: list[dict]) -> str:
    """The summary table of all networks' formulas (formulas/SUMMARY.md)."""
    lines = [
        "# Hour formulas: summary",
        "",
        "Generated by `python -m atlas.formula build`; see README.md for the schema.",
        "Hours are UTC. Peak/low and the daily factor are the network's all-country curve.",
        "A borrowed curve is the other networks' average, anchored on the network's measured hour;",
        "its peak-hour IPs are estimated (see README.md, Borrowed curve).",
        "",
        "| Network | Version | Hours with IPs | Requests but no IPs | Missing | Peak hour "
        "| Low hour | Peak-hour IPs | Day unique IPs | Daily factor | Countries "
        "| Network-curve fallbacks | Distinct test hours | Source runs |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    lines += [_row(document) for document in sorted(documents, key=lambda d: d["network"])]
    return "\n".join(lines) + "\n"
