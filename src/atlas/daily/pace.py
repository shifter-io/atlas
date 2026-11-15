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
limit, while the run's concurrency (and so its scope) stays the network's. A run that finds no
IP in the country stops after runner.ZERO_IPS_STOP answers, whatever its pace.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from psycopg.rows import tuple_row

from ..availability import census_peaks

SHARE = 0.5
FLOOR = 5
CEILING = 2000
PROBE_REQUESTS = 100
PROBE_CONCURRENCY = 20

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
    """A country's pace: {"concurrency": n}, or the probe's {"concurrency", "requests"}."""
    found = latest if latest is not None else census
    if found is None:
        return {"concurrency": min(PROBE_CONCURRENCY, cap), "requests": PROBE_REQUESTS}
    return {"concurrency": min(max(int(found * SHARE), FLOOR), CEILING, cap)}


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
