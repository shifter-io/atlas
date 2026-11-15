"""LOWAVAIL-1 (#1181) step 4b: which of a network's countries are low availability now.

The rows are LATEST's: a country's latest daily test (counts from 14-15 November 2026), its
previous and recent tests; the census peaks are the formulas'.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from psycopg.rows import dict_row
from test_country_verdicts import daily_test

from atlas.country_status import COUNTS, FAILING, LOW, NETWORK, NO_EXITS
from atlas.daily import expected
from atlas.storage import connect, ingest

pytest_plugins = ["test_storage"]

H = datetime(2026, 11, 15, 13, tzinfo=UTC)
HOUR = timedelta(hours=1)


def row(network, country, ips, statuses, errors=None, *, previous=None, recent=None, at=H):
    requests = sum(statuses.values()) + sum((errors or {}).values())
    return {
        "network": network,
        "country": country,
        "decided_at": at,
        "unique_ips": ips,
        "requests": requests,
        "completed": requests,
        "statuses": statuses,
        "errors": errors or {},
        "previous": previous,
        "recent": recent,
    }


CENSUS = {"rayobyte": {"CK": 2, "DE": 900, "US": 9000}, "fleetproxy": {"BI": 8, "NE": 184}}


def test_each_countrys_status_from_its_latest_test():
    rows = [
        row("rayobyte", "CK", 1, {"200": 71, "551": 2890}, {"timeout": 38}),  # "no exit"
        row("rayobyte", "DE", 700, {"200": 2990}, {"timeout": 9}),
        row("fleetproxy", "BI", 0, {}, {"timeout": 2999}),  # timeouts, census 8
        # census 184, its second test under 10, alone in its hour: its own failure, visible
        row("fleetproxy", "NE", 1, {"200": 27}, {"timeout": 2972}, previous=1, at=H + HOUR),
        row("webshare", "SC", 3, {"502": 2996}, previous=253, recent=253),  # a first bad day
    ]
    assert expected.statuses(rows, CENSUS) == {
        "rayobyte": {"CK": LOW, "DE": COUNTS},
        "fleetproxy": {"BI": NO_EXITS, "NE": FAILING},
        "webshare": {"SC": COUNTS},
    }
    assert expected.expected(rows, CENSUS) == {
        "rayobyte": frozenset({"CK"}),
        "fleetproxy": frozenset({"BI"}),
        "webshare": frozenset(),
    }


def test_the_network_guard_looks_at_the_big_countries_of_the_same_hour():
    down = {"200": 100}, {"timeout": 2900}
    rows = [
        row("rayobyte", "CK", 1, {"551": 2999}),
        row("rayobyte", "DE", 2, *down, previous=800),  # big, failing in the same hour
        row("rayobyte", "US", 4, *down, previous=9000),
        row("rayobyte", "GB", 3, *down, previous=500),
        row("rayobyte", "FR", 1, {"551": 2999}, at=H + HOUR),  # another hour
    ]
    found = expected.statuses(rows, CENSUS)["rayobyte"]
    assert found == {"CK": NETWORK, "DE": NETWORK, "US": NETWORK, "GB": NETWORK, "FR": LOW}
    two = [r for r in rows if r["country"] != "GB"]  # two big countries failing: no guard
    assert expected.statuses(two, CENSUS)["rayobyte"] == {
        "CK": LOW,
        "DE": COUNTS,
        "US": COUNTS,
        "FR": LOW,
    }


def test_the_report_line_names_the_first_countries():
    found = {"shifter": frozenset({"TV", "WF", "MH", "KI", "VU", "TO", "BV"}), "evomi": frozenset()}
    assert expected.line(found) == (
        "7 countries with low availability (no pulses): shifter: BV, KI, MH, TO, TV, ..."
    )
    assert expected.line(found, networks=("evomi",)) == (
        "0 countries with low availability (no pulses)"
    )
    assert expected.line({"geonode": frozenset({"BI"})}, shown=1) == (
        "1 countries with low availability (no pulses): geonode: BI"
    )


async def test_the_latest_tests_come_from_the_database(database, tmp_path):
    folder = tmp_path / "formulas"
    folder.mkdir()
    formula = '{"network": "fixture", "countries": {"FR": {"unique_ips": [2]}}}'
    (folder / "fixture.json").write_text(formula)
    for ips, country in ((12, "de"), (3, "fr")):
        assert ingest(await daily_test(tmp_path, ips, country), database)["imported"]
    with connect(database) as conn:
        conn.read_only = True
        cur = conn.cursor(row_factory=dict_row)
        later = datetime(2999, 1, 1, tzinfo=UTC)
        assert expected.read(cur, folder, later) == {"fixture": frozenset({"FR"})}
        assert expected.read(cur, folder, datetime(2000, 1, 1, tzinfo=UTC)) == {}
        rows = cur.execute(expected.LATEST, {"as_of": later}).fetchall()

    class Db:  # the orchestrator's database: dict rows through an async connection
        def connection(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, query, params):
            assert query == expected.LATEST and params == {"as_of": later}
            return self

        async def fetchall(self):
            await asyncio.sleep(0)
            return rows

    assert await expected.aread(Db(), folder, later) == {"fixture": frozenset({"FR"})}
