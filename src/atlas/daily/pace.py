"""LOWAVAIL-1 (#1181) step 3: the pace of a country's daily test follows its size.

The owner's rule (2026-11-15). A country's daily test starts at half of the unique IPs its latest
daily test found (SHARE), at least FLOOR and at most CEILING requests at once, and never above
its network's concurrency for the hour (the template's, or a budgeted network's share:
daily.budget). Before a country's first daily test its October census peak (the formulas' best
hour, availability.census_peaks) stands in. A country neither tested nor in the census gets a
small probe first: PROBE_REQUESTS requests, PROBE_CONCURRENCY at once. So the about 40 tiny
countries of each network cost a few hundred requests a day instead of thousands.

The pace is written per country into the network's hour manifest (`country_pace`,
onboarding.country_pace) and the worker applies it as the run's start_concurrency, its in-flight
limit, while the run's concurrency (and so its scope) stays the network's. During the run the
limit may grow every few seconds to half of the IPs found so far (runner.GROW_SECONDS), never
above the network's concurrency. A run that finds no IP in the country stops after
runner.ZERO_IPS_STOP answers, whatever its pace.

LOWAVAIL-PACE-2 (#1247): a low-availability country (its latest test, or before its first its
census peak, under runner.LOW_IPS unique IPs) also gets a hard cap, LOW_REQUESTS requests or
LOW_SECONDS, whichever comes first (`low_requests`, `low_seconds`: RunConfig's). It applies only
while the run has found fewer than LOW_IPS IPs: a country that finds more runs on under today's
rules. A cell runs its low-availability countries side by side (onboarding.rounds.LOW_PARALLEL),
so a cell of a dozen tiny territories takes about a minute, not half an hour.

LOWAVAIL-PACE-3 (#1290): a country that is not low availability (LOW_IPS or more) starts at
least at REAL_FLOOR requests at once: at half its IPs a country with 10 to 50 IPs ran at 5 to 25
at once and needed minutes for its minimum sample (RunConfig.min_sample answers); on 16 Nov at
21:00 iproyal blr1's 15 countries took 2,170 s instead of 227 s the day before. Its pace then
doubles every few seconds up to half the IPs it finds (runner.RAMP_SECONDS). Every country also
gets a time budget (`seconds`, its run's cap_seconds, stop_reason `cap`): BUDGET_BASE plus
BUDGET_REQUEST_SECONDS for every request its unique-ratio stop needs (its minimum sample, or
twice its IPs) per connection, at most the 60-minute country cap. That is about twice what the
rule needed in the daily tests of 1 to 15 Nov (none of their 4,755 unique-ratio runs would have
reached it), so it ends only a test that hangs, while a cell stays bounded with the round's
soft limit (onboarding.rounds.SOFT_LIMIT).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from psycopg.rows import tuple_row

from ..availability import census_peaks
from ..models import RunConfig
from ..onboarding.scheduler import COUNTRY_CAP_SECONDS
from ..runner import LOW_IPS, PACE_SHARE

SHARE = PACE_SHARE  # the same share the run grows by (runner.PACE_SHARE)
FLOOR = 5
REAL_FLOOR = 200  # a country with LOW_IPS or more (LOWAVAIL-PACE-3)
CEILING = 2000
PROBE_REQUESTS = 100
PROBE_CONCURRENCY = 20
LOW_REQUESTS = 300
LOW_SECONDS = 30
# A country's time budget: BUDGET_BASE s, plus BUDGET_REQUEST_SECONDS per request its stop needs
# per connection (a request took up to about 15 s per connection in the daily tests of 1 to
# 15 Nov, the median about 6 s; the base covers a run's start and its last answers).
BUDGET_BASE = 120
BUDGET_REQUEST_SECONDS = 8
MIN_SAMPLE = RunConfig.__dataclass_fields__["min_sample"].default  # every daily test's

# Each network's latest daily test per country decided by %(as_of)s: its unique IPs.
LATEST = (
    "SELECT DISTINCT ON (p.provider,v.country_code) p.provider,v.country_code::text,v.unique_ips "
    "FROM country_verdicts v JOIN products p USING(product_id) WHERE v.decided_at<=%(as_of)s "
    "ORDER BY p.provider,v.country_code,v.decided_at DESC"
)


@dataclass(frozen=True)
class History:
    """What the pace starts from: {network: {country: unique IPs}} of the latest daily tests
    and of the October census peaks."""

    latest: dict = field(default_factory=dict)
    census: dict = field(default_factory=dict)


def start(latest: int | None, census: int | None, cap: int) -> dict:
    """A country's pace: {"concurrency": n}, with its low-availability cap {"low_requests",
    "low_seconds"} under LOW_IPS, or the probe's {"concurrency", "requests"}; each with its
    time budget {"seconds"}."""
    found = latest if latest is not None else census
    if found is None:
        probe = min(PROBE_CONCURRENCY, cap)
        return {"concurrency": probe, "requests": PROBE_REQUESTS} | budget(PROBE_REQUESTS, probe)
    floor = FLOOR if found < LOW_IPS else REAL_FLOOR
    pace = {"concurrency": min(max(int(found * SHARE), floor), CEILING, cap)}
    if found < LOW_IPS:
        pace |= {"low_requests": LOW_REQUESTS, "low_seconds": LOW_SECONDS}
    return pace | budget(max(MIN_SAMPLE, 2 * found), pace["concurrency"])


def budget(requests: int, concurrency: int) -> dict:
    """A country's time budget, {"seconds": n}: about twice what `requests` take at
    `concurrency` at once, at most the country cap."""
    seconds = BUDGET_BASE + math.ceil(BUDGET_REQUEST_SECONDS * requests / concurrency)
    return {"seconds": min(seconds, int(COUNTRY_CAP_SECONDS))}


def country_pace(network: str, due: dict, history: History, cap: int) -> dict:
    """{country: pace} of the network's countries due this hour (its {region: [codes]})."""
    latest, census = history.latest.get(network, {}), history.census.get(network, {})
    return {
        country: start(latest.get(country), census.get(country), cap)
        for countries in due.values()
        for country in countries
    }


def history(formulas: Path, database_file: Path | None, as_of: datetime) -> History:
    """The census peaks, and the latest daily tests read in a read-only transaction (none
    without a database file, or when the database cannot be reached: the hour still runs and
    its results wait on disk for the import, as before)."""
    peaks = census_peaks(formulas)
    if database_file is None:
        return History(census=peaks)
    from ..storage import connect, psycopg

    found = {}
    try:
        with connect(database_file) as conn:
            conn.read_only = True
            cursor = conn.cursor(row_factory=tuple_row)
            for network, country, ips in cursor.execute(LATEST, {"as_of": as_of}):
                found.setdefault(network, {})[country] = ips
    except psycopg.OperationalError:
        return History(census=peaks)
    return History(found, peaks)
