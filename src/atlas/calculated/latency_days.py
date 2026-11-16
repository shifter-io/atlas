"""LATENCY-DELTA-24H-1: the 24h latency change, the headline latency p50 of the last whole day
against the day before it (the 24h window's latency_day_delta). The headline is the pulses'
latency of normal requests (HEADLINE-PULSE-1): the daily tests' latency under load stays out.

Every country has one daily test a day, at its own hour, so only a whole day holds every country:
a shorter part of a day compares another mix of countries, and the mix alone moves the latency.
The days are UTC calendar days. The comparison moves to the newest pair at FREEZE_HOUR, once the
day's last daily tests have finished and been ingested, so every build of a day publishes the
same pair and a half-tested day never makes a change.

Each day passes the figure's own rule (time slots with enough successes, as under_load() and the
light-load latency) and has enough tested countries: at least COUNTRY_SHARE of the countries
tested over the two days, each with min_latency_successes successes. Otherwise pct is None with a
reason: NEEDS_TWO_DAYS when the network has no test before the earlier day (it is new),
DAY_NOT_COVERED otherwise.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

from .latency import blended
from .underload import light_votes, time_slots

FREEZE_HOUR = 3  # UTC: the hour each day's comparison is made, over the two days before it
COUNTRY_SHARE = 0.9
NEEDS_TWO_DAYS = "needs_two_days"
DAY_NOT_COVERED = "day_not_covered"
DAY = timedelta(days=1)


def day_pair(as_of):
    """The start of the earlier and of the last whole day the build at as_of compares."""
    frozen = (as_of - timedelta(hours=FREEZE_HOUR)).astimezone(UTC)
    last = datetime(frozen.year, frozen.month, frozen.day, tzinfo=UTC) - DAY
    return last - DAY, last


def _light(row):
    """Whether a row counts in the latency of normal requests (underload.time_slots())."""
    return not row.get("loaded") and row.get("light", True)


def _day(rows, start, minimum):
    """One day's headline p50 (each slot with `minimum` light-load successes weighs the same)
    and its countries tested with `minimum` light-load successes."""
    own = [r for r in rows if start <= r["bucket_start"] < start + DAY]
    successes = Counter()
    for r in own:
        if _light(r):
            successes[r["country"] or ""] += r["counters"]["successes"]
    votes = light_votes(time_slots(own), minimum)
    return {
        "start": start,
        "end": start + DAY,
        "p50": blended(h for *_, h in votes)["p50"],
        "countries": sorted(c for c, n in successes.items() if n >= minimum),
    }


def window_delta(rows, target, as_of, minimum):
    """latency_day_delta() of a 24h window ending at as_of: rows are its network's bucket rows,
    target is the latency target, whose headline it follows."""
    return latency_day_delta([r for r in rows if r["target"] == target], as_of, minimum)


def latency_day_delta(rows, as_of, minimum):
    """The 24h window's latency_day_delta: rows are the latency target's bucket rows. pct is the
    last day's p50 of normal requests against the earlier day's, in percent (positive: slower),
    None with a reason when a day does not pass (module docstring)."""
    first, last = day_pair(as_of)
    days = [_day(rows, start, minimum) for start in (first, last)]
    tested = set(days[0]["countries"]) | set(days[1]["countries"])
    covered = tested and all(
        d["p50"] and len(d["countries"]) >= COUNTRY_SHARE * len(tested) for d in days
    )
    pct = reason = None
    if covered:
        pct = (days[1]["p50"] - days[0]["p50"]) / days[0]["p50"] * 100
    elif any(r["bucket_start"] < first for r in rows):
        reason = DAY_NOT_COVERED
    else:
        reason = NEEDS_TWO_DAYS
    return {
        "pct": pct,
        "reason": reason,
        "basis": "normal",
        "computed_at": last + DAY + timedelta(hours=FREEZE_HOUR),
        "days": [d | {"countries": len(d["countries"])} for d in days],
    }
