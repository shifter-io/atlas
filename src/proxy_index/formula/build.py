"""The public hour formula of one network, built from its verified campaign counts.

A curve is 24 UTC clock hours of unique IPs. Its peak hour has the most unique IPs (tie: more
request rows, then the earlier hour) and its low hour the fewest (tie: fewer request rows, then
the earlier hour); a missing hour (no request at all) is neither. Then

- hour factor = the hour's unique IPs / the peak hour's unique IPs (the peak is exactly 1.0);
- daily factor = whole-day unique IPs (union over the hours) / the peak hour's unique IPs.

Factors are stored at full float precision, so round(factor * peak IPs) gives back each measured
hour and the whole day exactly. A country whose peak hour has fewer than min_peak_ips unique IPs,
or that has IPs in fewer than min_hours hours (capped at the network's own hours with IPs), uses
the network's all-country curve instead, marked curve "network" with the reason. Each country's
test hour (the daily test schedule) is the peak hour of the curve it uses.
"""

from __future__ import annotations

import json
import re

from proxy_index.formula.aggregate import HOURS, FormulaError

SCHEMA = "proxy-index-hour-formula"
SCHEMA_VERSION = 1
DEFAULT_MIN_PEAK_IPS = 100
DEFAULT_MIN_HOURS = 20
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
    measured = [value != "missing" for value in status]
    return {
        "status": status,
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


def _country(own: dict, network: dict, min_peak_ips: int, min_hours: int) -> dict:
    measured = [value != "missing" for value in network["status"]]
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
    floor = min(min_hours, network["hours_with_ips"])
    reason = _fallback_reason(entry, peak_ips, min_peak_ips, floor)
    used = network if reason else measured_curve
    entry.update(
        curve="network" if reason else "country",
        fallback_reason=reason,
        hour_factors=used["hour_factors"],
        daily_factor=used["daily_factor"],
        test_hour=used["peak_hour"],
    )
    return entry


def build_formula(counts: dict, version: int, min_peak_ips: int, min_hours: int) -> dict:
    """The public formula document of one network's counts (see formulas/README.md)."""
    if len(counts["hours"]) != len(HOURS):
        raise FormulaError(f"{counts['network']}: counts need 24 hours")
    network = _network_curve(counts)
    countries = {
        code: _country(own, network, min_peak_ips, min_hours)
        for code, own in sorted(counts["countries"].items())
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
        "rules": {
            "hour": "UTC clock hour of the round's scheduled start",
            "country": "requested country (the test's country parameter)",
            "requests": "request rows: successful requests with an exit IP",
            "min_peak_unique_ips": min_peak_ips,
            "min_hours_with_ips": min_hours,
        },
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
    return (
        f"| {document['network']} | {document['formula_version']} | {network['hours_with_ips']} "
        f"| {no_ip} | {missing} | {network['peak_hour']:02d} | {network['low_hour']:02d} "
        f"| {network['unique_ips'][network['peak_hour']]:,} | {network['day_unique_ips']:,} "
        f"| {network['daily_factor']:.3f} | {len(countries)} | {fallback} | {test_hours} "
        f"| {', '.join(document['source']['campaign_runs'])} |"
    )


def summary_markdown(documents: list[dict]) -> str:
    """The summary table of all networks' formulas (formulas/SUMMARY.md)."""
    lines = [
        "# Hour formulas: summary",
        "",
        "Generated by `python -m proxy_index.formula build`; see README.md for the schema.",
        "Hours are UTC. Peak/low and the daily factor are the network's all-country curve.",
        "",
        "| Network | Version | Hours with IPs | Requests but no IPs | Missing | Peak hour "
        "| Low hour | Peak-hour IPs | Day unique IPs | Daily factor | Countries "
        "| Network-curve fallbacks | Distinct test hours | Source runs |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    lines += [_row(document) for document in sorted(documents, key=lambda d: d["network"])]
    return "\n".join(lines) + "\n"
