"""COUNTRY-MIN-IPS-1: each imported daily test is one verdict on whether its country counts.

A daily test (summary test_kind 'daily', as daily ingest writes it) gets one country_verdicts row
in its import's transaction: its network, its requested country, its start and its unique IPs.
Pulses and other runs get none, and a replay adds none. Whether a verdict counts is decided when
read (verdicts.RULE, schema v20), from its unique IPs and the minimum it is read with (the
methodology's country_min_ips: 1 since #1253, 10 before), so these tests read it under 1, 2 and 10.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from census_pulse_cases import product
from storage_cases import measurement

from atlas import storage, verdicts
from atlas.models import Outcome
from atlas.storage import connect, ingest, ingest_many

pytest_plugins = ["test_storage"]

TEN = 10  # #1137's minimum; the methodology's country_min_ips is read from the file elsewhere
# Each verdict with whether it counts under %(min_ips)s, by the window form (ruled()), and the
# same by the per-verdict form (counts_of()): the two must always agree.
VERDICTS = (
    "SELECT v.product_id,v.country_code,v.decided_at=t.started_at,v.unique_ips,v.counts,"
    f"{verdicts.counts_of('w')} FROM ({verdicts.ruled('<%(until)s')}) v "
    "JOIN country_verdicts w USING(run_key) JOIN test_runs t ON t.run_key=v.run_key "
    "ORDER BY t.run_id::text"
)


def rotating(ips):
    """A transport whose answers come from `ips` distinct documentation addresses in turn."""

    class Rotating:
        def __init__(self, config):
            self.count = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, target, *, context=None):
            self.count += 1
            address = f"198.51.100.{self.count % ips + 1}"
            return Outcome(10.1, ip=address, status=200, response_body_bytes=8)

    return Rotating


async def daily_test(tmp_path, ips, country="de", *, daily=True, provider="fixture"):
    """A run directory of a daily test of `country` that found `ips` unique IPs (a plain run with
    daily=False), marked as daily ingest marks it."""
    directory = await measurement(
        tmp_path / f"{country}-{ips}-{daily}",
        requests=ips * 2,
        requested_country=country,
        provider=provider,
        transport_factory=rotating(ips),
    )
    if daily:
        path = Path(directory) / "summary.json"
        summary = json.loads(path.read_text())
        path.write_text(json.dumps(summary | {"test_kind": "daily"}))
    return directory


def product_id(database, provider="fixture"):
    with connect(database) as conn:
        query = "SELECT product_id FROM products WHERE provider=%s"
        return conn.execute(query, (provider,)).fetchone()[0]


def rows(database, rule=TEN):
    """VERDICTS' rows under the minimum `rule`, each counted alike by both forms of the rule."""
    with connect(database) as conn:
        found = conn.execute(VERDICTS, {"until": "infinity", "min_ips": rule}).fetchall()
    assert all(row[4] is row[5] for row in found)
    return [row[:5] for row in found]


def plain(database, query):
    with connect(database) as conn:
        return conn.execute(query).fetchall()


async def test_a_daily_test_records_its_country_verdict(database, tmp_path):
    many, few = await daily_test(tmp_path, 12, "de"), await daily_test(tmp_path, 6, "fr")
    assert ingest(many, database)["imported"] and ingest(few, database)["imported"]
    product = product_id(database)
    found = sorted(rows(database))
    assert found == [(product, "DE", True, 12, True), (product, "FR", True, 6, False)]
    assert not ingest(many, database)["imported"]  # a replay adds no second verdict
    assert sorted(rows(database)) == found
    # The same stored verdicts under a minimum of 2: both count, nothing rewritten.
    assert sorted(rows(database, 2)) == [
        (product, "DE", True, 12, True),
        (product, "FR", True, 6, True),
    ]


async def test_only_daily_tests_get_a_verdict(database, tmp_path):
    plain = await daily_test(tmp_path, 12, "de", daily=False)
    nowhere = await daily_test(tmp_path, 12, None)  # no requested country: no verdict
    edge = await daily_test(tmp_path, TEN, "es")
    below = await daily_test(tmp_path, TEN - 1, "it")
    results = ingest_many([plain, nowhere, edge, below], database)
    assert all(result["imported"] for result in results)
    product = product_id(database)
    assert sorted(rows(database)) == [
        (product, "ES", True, TEN, True),
        (product, "IT", True, TEN - 1, False),
    ]


async def test_each_network_keeps_its_own_verdicts(database, tmp_path):
    first = await daily_test(tmp_path, 12, "de", provider="fixture")
    second = await daily_test(tmp_path, 3, "de", provider="other")
    ingest_many([first, second], database)
    found = {(p, counts) for p, _, _, _, counts in rows(database, 4)}
    assert found == {(product_id(database), True), (product_id(database, "other"), False)}


def test_no_stored_column_decides_whether_a_verdict_counts():
    schema = Path(storage.__file__).with_name("schema.sql").read_text()
    table = schema[schema.index("CREATE TABLE IF NOT EXISTS country_verdicts") :]
    assert " counts " not in table[: table.index(");")]  # decided when read since v20
    assert f"end_reason IN ('{verdicts.END_REASON}','not_measured')" in schema
    two_days = Path(storage.__file__).with_name("migrate_v18_v19.sql").read_text()
    assert f"h.recent >= {verdicts.BIG}" in two_days  # the applied v19 step, as it was
    assert f"RANGE BETWEEN interval '{verdicts.RECENT}' PRECEDING" in two_days
    assert f"interval '{verdicts.RECENT}'" in verdicts.counts_of("v")
    assert "%(min_ips)s" in verdicts.RULE and "10" not in verdicts.RULE


DAY = timedelta(days=1)
START = datetime(2026, 11, 10, 17, tzinfo=UTC)
# A country per IP count, each with one daily test: whether it counts and its status under each
# minimum (COUNTRY-MIN-IPS-1, #1253: 0 is no exits, under the minimum low availability).
TABLE = {
    1: {0: "no_exits", 1: "counts", 2: "counts", 9: "counts", 10: "counts"},
    2: {0: "no_exits", 1: "too_few_ips", 2: "counts", 9: "counts", 10: "counts"},
    10: {0: "no_exits", 1: "too_few_ips", 2: "too_few_ips", 9: "too_few_ips", 10: "counts"},
}
COUNTRY = {0: "AD", 1: "BE", 2: "CH", 9: "DK", 10: "EE"}


def stored(database, history):
    """Product alpha's verdicts: {country: [IPs of each day's test]}, a day apart."""
    with connect(database) as conn:
        alpha = product(conn, "alpha")
        key = 0
        for country, days in history.items():
            for day, ips in enumerate(days):
                key -= 1
                conn.execute(
                    "INSERT INTO country_verdicts(product_id,country_code,decided_at,run_key,"
                    "unique_ips) VALUES (%s,%s,%s,%s,%s)",
                    (alpha, country, START + day * DAY, key, ips),
                )
    return alpha


@pytest.mark.parametrize("rule", sorted(TABLE))
def test_each_status_under_a_minimum_of_1_2_and_10(database, rule):
    alpha = stored(database, {COUNTRY[ips]: [ips] for ips in COUNTRY})
    with connect(database) as conn:
        latest = verdicts.latest(conn, START + 3 * DAY, rule)[alpha]
        spans = verdicts.by_country(verdicts.spans(conn, START, START + DAY, rule))
    found = {ips: verdicts.status([latest[COUNTRY[ips]]])["status"] for ips in COUNTRY}
    assert found == TABLE[rule]
    assert verdicts.status([]) == {"status": "no_daily_test", "unique_ips": None}  # untested
    assert sorted(spans.get(alpha, {})) == sorted(
        COUNTRY[ips] for ips, status in TABLE[rule].items() if status == "counts"
    )


@pytest.mark.parametrize("rule", sorted(TABLE))
def test_two_days_in_a_row_follows_the_minimum(database, rule):
    # A normally big country (60) keeps counting on its first test under the minimum (0), not on
    # a second; a country never big (40) stops at once; one under the minimum before its first
    # test under it does not count on it either.
    alpha = stored(database, {"DE": [60, 0, 0], "FR": [40, 0], "IT": [rule - 1 or 0, 0]})
    with connect(database) as conn:
        days = [verdicts.latest(conn, START + n * DAY, rule)[alpha] for n in range(3)]
    assert [day["DE"][0] for day in days] == [True, True, False]
    assert [day["FR"][0] for day in days[:2]] == [True, False]
    assert days[1]["IT"][0] is False


async def test_a_normally_big_country_counts_on_its_first_day_under_ten(database, tmp_path):
    # LOWAVAIL-1, two days in a row: 60, then 6 (still counts), then 4 (does not); a small
    # country (8, then 12, then 3) never counts under 10.
    for day, (big, small) in enumerate([(60, 8), (6, 12), (4, 3)]):
        directories = [
            await daily_test(tmp_path / str(day), big, "de"),
            await daily_test(tmp_path / str(day), small, "tv"),
        ]
        assert all(result["imported"] for result in ingest_many(directories, database))
    found = {}
    for _, country, _, ips, counts in sorted(rows(database), key=lambda r: (r[1], r[3])):
        found.setdefault(country, {})[ips] = counts
    assert found == {"DE": {4: False, 6: True, 60: True}, "TV": {3: False, 8: False, 12: True}}


async def test_one_import_counts_its_own_earlier_tests(database, tmp_path):
    # A rebuild or backfill imports several tests of one country together.
    tests = [await daily_test(tmp_path / str(n), ips, "de") for n, ips in enumerate((60, 6, 4))]
    assert all(result["imported"] for result in ingest_many(tests, database))
    assert [(r[3], r[4]) for r in sorted(rows(database), key=lambda r: -r[3])] == [
        (60, True),
        (6, True),
        (4, False),
    ]


async def test_a_verdict_outlives_its_run(database, tmp_path):
    """No foreign key ties a verdict to its run: the archive purge deletes old runs
    (archive/purge.py) and the verdict still holds until the next daily test."""
    ingest(await daily_test(tmp_path, 12), database)
    keys = (
        "SELECT confrelid::regclass::text FROM pg_constraint "
        "WHERE conrelid='country_verdicts'::regclass AND contype='f'"
    )
    assert plain(database, keys) == [("products",)]


def test_a_requested_countrys_status_names_low_availability_and_no_exits():
    # LOWAVAIL-1: too_few_ips is "low availability"; no_exits is the same with no IP found.
    assert verdicts.status([(False, 0)]) == {"status": "no_exits", "unique_ips": 0}
    assert verdicts.status([(False, 0), (False, 3)]) == {"status": "too_few_ips", "unique_ips": 3}
    assert verdicts.status([(True, 0), (False, 4)]) == {"status": "counts", "unique_ips": 0}
    assert verdicts.status([]) == {"status": "no_daily_test", "unique_ips": None}
