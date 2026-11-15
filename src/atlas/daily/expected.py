"""LOWAVAIL-1 (#1181) step 4b: which of a network's countries are low availability now.

A country's status (country_status.status) comes from its latest daily test: its unique IPs and
how its requests ended, its previous daily test, the most IPs of its daily tests of the
verdicts.RECENT_DAYS before, its October census peak (the formulas) and the network's "no exit"
answers (its grammar). The network guard (country_status.network_problem) looks at the network's
normally big countries tested in the same hour. A country whose status is expected (low
availability or no exits, country_status.EXPECTED) is not an error anywhere: the hourly pulses
skip it (pulses.pulse_hour's `skip`; the daily test is its check, so it counts again from the
next calculation once a daily test finds 10 IPs), and the hour report lists it on its own
"expected" line, never as a failure.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..availability import census_peaks, no_exit
from ..country_status import EXPECTED, DailyTest, network_problem, normally_big, status
from ..verdicts import RECENT

# Each network's latest daily test per country decided by %(as_of)s: its IPs and answers, and the
# IPs of the tests before it (the previous one, and the most of the RECENT before it).
LATEST = (
    "SELECT p.provider AS network,v.country_code::text AS country,v.decided_at,v.unique_ips,"
    "coalesce((t.summary->>'started')::bigint,0) AS requests,"
    "coalesce((t.summary->>'completed')::bigint,0) AS completed,"
    "coalesce(t.summary->'status_codes','{}') AS statuses,"
    "coalesce(t.summary->'errors','{}') AS errors,"
    "(SELECT w.unique_ips FROM country_verdicts w WHERE w.product_id=v.product_id "
    "AND w.country_code=v.country_code AND w.decided_at<v.decided_at "
    "ORDER BY w.decided_at DESC LIMIT 1) AS previous,"
    "(SELECT max(w.unique_ips) FROM country_verdicts w WHERE w.product_id=v.product_id "
    "AND w.country_code=v.country_code AND w.decided_at<v.decided_at "
    f"AND w.decided_at>=v.decided_at-interval '{RECENT}') AS recent "
    "FROM (SELECT DISTINCT ON (product_id,country_code) * FROM country_verdicts "
    "WHERE decided_at<=%(as_of)s ORDER BY product_id,country_code,decided_at DESC) v "
    "JOIN products p USING(product_id) LEFT JOIN test_runs t USING(run_key)"
)


def statuses(rows: list[dict], census: dict) -> dict[str, dict[str, str]]:
    """{network: {country: status}} of LATEST's rows, with the census peaks ({network:
    {country: IPs}}, availability.census_peaks)."""
    tests, hours = {}, {}
    for row in rows:
        test = DailyTest.of(
            row["unique_ips"], row["requests"], row["statuses"], row["errors"], row["completed"]
        )
        peak = census.get(row["network"], {}).get(row["country"])
        tests[(row["network"], row["country"])] = (row, test, peak)
        if normally_big(row["previous"], row["recent"], peak):
            hour = (row["network"], row["decided_at"].replace(minute=0, second=0, microsecond=0))
            hours.setdefault(hour, []).append(test)
    found: dict[str, dict[str, str]] = {}
    for (network, country), (row, test, peak) in tests.items():
        hour = row["decided_at"].replace(minute=0, second=0, microsecond=0)
        down = network_problem(hours.get((network, hour), []))
        found.setdefault(network, {})[country] = status(
            test, no_exit(network), row["previous"], peak, down, row["recent"]
        )
    return found


def expected(rows: list[dict], census: dict) -> dict[str, frozenset[str]]:
    """{network: its countries whose status is expected (low availability, no exits)}."""
    return {
        network: frozenset(c for c, s in countries.items() if s in EXPECTED)
        for network, countries in statuses(rows, census).items()
    }


def read(cur, formulas: Path, as_of: datetime) -> dict[str, frozenset[str]]:
    """expected() at `as_of` through a dict-row cursor (the hour report's read-only one)."""
    return expected(cur.execute(LATEST, {"as_of": as_of}).fetchall(), census_peaks(formulas))


async def aread(db, formulas: Path, as_of: datetime) -> dict[str, frozenset[str]]:
    """expected() at `as_of` from the orchestrator's database (dict rows)."""
    async with db.connection() as conn:
        rows = await (await conn.execute(LATEST, {"as_of": as_of})).fetchall()
    return expected(rows, census_peaks(formulas))


def line(found: dict, networks=None, shown: int = 5) -> str:
    """The hour report's "expected" line: how many countries have low availability, and the
    first `shown` of each network (those of `networks` only, when given)."""
    named = {
        network: sorted(countries)
        for network, countries in sorted(found.items())
        if countries and (not networks or network in networks)
    }
    parts = [
        f"{network}: {', '.join(c[:shown])}{', ...' if len(c) > shown else ''}"
        for network, c in named.items()
    ]
    count = sum(len(c) for c in named.values())
    return f"{count} countries with low availability (no pulses)" + (
        f": {'; '.join(parts)}" if parts else ""
    )
