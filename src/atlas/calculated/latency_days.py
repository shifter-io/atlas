"""LATENCY-DELTA-24H-1: the 24h latency change, the headline latency p50 of the last whole day
against the day before it (the 24h window's latency_day_delta). LATENCY-DELTA-PERIOD-1: the 7d
and 30d windows' compare the last 7 (30) whole days with the 7 (30) before the same way
(PERIOD_DAYS). LATENCY-DELTA-FALLBACK-1: a window whose two periods do not pass compares the
next shorter ones (FALLBACK: 30d the last 7 whole days, then the last day; 7d the last day), so
it is null only when the day rule is. The calculator reads 30 days of raw rows (RAW_DAYS), so
the 30d change's periods are made of per-day records, the 30 days before the raw rows' from the
per-day store, and compared by compare() (latency_store.py, LATENCY-DAY-STORE-1). The headline
is the pulses' latency of normal requests (HEADLINE-PULSE-1): the daily tests' latency under load
stays out.

Every country has one daily test a day, at its own hour, so only a whole day holds every country:
a shorter part of a day compares another mix of countries, and the mix alone moves the latency.
The days are UTC calendar days. The comparison moves to the newest pair at FREEZE_HOUR, once the
day's last daily tests have finished and been ingested, so every build of a day publishes the
same pair and a half-tested day never makes a change.

Each period passes the figure's own rule (time slots with enough successes, as under_load() and
the light-load latency) and has enough tested countries: at least COUNTRY_SHARE of the countries
tested over the two periods, each with min_latency_successes successes in it, and the network was
tested by the earlier period's first day (so a longer period is never compared from a part of it).
Otherwise pct is None with a reason: NEEDS_TWO_DAYS when the network has no test before the
earlier period (it is new), DAY_NOT_COVERED otherwise; with FALLBACK only the day rule's reason
is served. LATENCY-ALWAYS-1 (the owner's rule: always a value): a pair that fails only the
country rule is compared on the countries tested in both periods (COMMON_SHARE of the countries
tested over the two, at least COMMON_MIN; "scope": "common") before the next shorter one,
and a window left without a value keeps the last one computed (latency_store.carry()).
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from functools import partial

from .latency import blended
from .underload import light_votes, time_slots

FREEZE_HOUR = 3  # UTC: the hour each day's comparison is made, over the whole days before it
COUNTRY_SHARE = 0.9
NEEDS_TWO_DAYS = "needs_two_days"
DAY_NOT_COVERED = "day_not_covered"
DAY = timedelta(days=1)
PERIOD_DAYS = {"24h": 1, "7d": 7, "30d": 30}  # each window's period, in whole days
# The common-country comparison: the countries tested in both periods are at least this share of
# the countries tested over the two, and at least COMMON_MIN of them.
COMMON_SHARE = 0.5
COMMON_MIN = 5
# The periods a window of `days` whole days compares, the longest first (LATENCY-DELTA-FALLBACK-1).
FALLBACK = {1: (1,), 7: (7, 1), 30: (30, 7, 1)}


def day_pair(as_of, days=1):
    """The start of the earlier and of the last period of `days` whole days the build at as_of
    compares."""
    frozen = (as_of - timedelta(hours=FREEZE_HOUR)).astimezone(UTC)
    last = datetime(frozen.year, frozen.month, frozen.day, tzinfo=UTC) - days * DAY
    return last - days * DAY, last


def _light(row):
    """Whether a row counts in the latency of normal requests (underload.time_slots())."""
    return not row.get("loaded") and row.get("light", True)


def _day(rows, minimum, start, days, countries=None):
    """The period of `days` whole days from start (of only `countries`' rows when given): its
    headline p50 (each slot with `minimum` light-load successes weighs the same) and its
    countries tested with `minimum` light-load successes."""
    span = days * DAY
    own = [r for r in rows if start <= r["bucket_start"] < start + span]
    if countries is not None:
        own = [r for r in own if (r["country"] or "") in countries]
    successes = Counter()
    for r in own:
        if _light(r):
            successes[r["country"] or ""] += r["counters"]["successes"]
    votes = light_votes(time_slots(own), minimum)
    return {
        "start": start,
        "end": start + span,
        "p50": blended(h for *_, h in votes)["p50"],
        "countries": sorted(c for c, n in successes.items() if n >= minimum),
    }


def window_delta(rows, target, as_of, minimum, days=1):
    """latency_day_delta() of a window ending at as_of: rows are its network's bucket rows,
    target is the latency target, whose headline it follows."""
    return latency_day_delta([r for r in rows if r["target"] == target], as_of, minimum, days)


def latency_day_delta(rows, as_of, minimum, days=1):
    """A window's latency_day_delta, over periods of `days` whole days, else the longest shorter
    ones of FALLBACK[days] that pass, each on all its countries, else on the common ones
    (_common()): rows are the latency target's bucket rows. pct is the last period's p50 of
    normal requests against the earlier period's, in percent (positive: slower), None with the
    day rule's reason when no pair passes (module docstring). "days" holds the two periods."""
    since = min((r["bucket_start"] for r in rows), default=None)
    period = partial(_day, rows, minimum)
    for n in FALLBACK[days]:
        pair = day_pair(as_of, n)
        periods = [period(start, n) for start in pair]
        found = compare(periods, since)
        if found["pct"] is None and _tested_by(since, periods[0]):
            found = _common(period, pair, n, periods, since) or found
        if found["pct"] is not None:
            break
    return found


def _common(period, pair, days, periods, since):
    """compare() of the pair's periods made of only the countries tested in both (periods: the
    pair on all countries), None when they are too few or still give no value."""
    both = set(periods[0]["countries"]) & set(periods[1]["countries"])
    tested = set(periods[0]["countries"]) | set(periods[1]["countries"])
    if len(both) < max(COMMON_MIN, COMMON_SHARE * len(tested)):
        return None
    found = compare([period(start, days, both) for start in pair], since)
    return found | {"scope": "common"} if found["pct"] is not None else None


def _tested_by(since, first):
    """Whether a network first tested at `since` was tested by the period's first day."""
    return since is not None and since < first["start"] + DAY


def compare(periods, since):
    """latency_day_delta() of the earlier and the last period (each {"start", "end", "p50",
    "countries": the tested countries}) of a network first tested at `since` (None: never)."""
    first, last = periods
    tested = set(first["countries"]) | set(last["countries"])
    covered = (
        _tested_by(since, first)
        and tested
        and all(d["p50"] and len(d["countries"]) >= COUNTRY_SHARE * len(tested) for d in periods)
    )
    pct = reason = None
    if covered:
        pct = (last["p50"] - first["p50"]) / first["p50"] * 100
    elif since is not None and since < first["start"]:
        reason = DAY_NOT_COVERED
    else:
        reason = NEEDS_TWO_DAYS
    return {
        "pct": pct,
        "reason": reason,
        "basis": "normal",
        "scope": "all",
        "computed_at": last["end"] + timedelta(hours=FREEZE_HOUR),
        "days": [d | {"countries": len(d["countries"])} for d in periods],
    }
