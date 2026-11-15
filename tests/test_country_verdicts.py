"""COUNTRY-MIN-IPS-1: each imported daily test is one verdict on whether its country counts.

A daily test (summary test_kind 'daily', as daily ingest writes it) gets one country_verdicts row
in its import's transaction: its network, its requested country, its start, its unique IPs and
whether they reach verdicts.MIN_IPS (10). Pulses and other runs get none, and a replay adds none.
"""

import json
from pathlib import Path

from storage_cases import measurement

from atlas import storage, verdicts
from atlas.models import Outcome
from atlas.storage import connect, ingest, ingest_many

pytest_plugins = ["test_storage"]

VERDICTS = (
    "SELECT v.product_id,v.country_code,v.decided_at=t.started_at,v.unique_ips,v.counts "
    "FROM country_verdicts v JOIN test_runs t USING(run_key) ORDER BY t.run_id::text"
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


def rows(database, query=VERDICTS):
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


async def test_only_daily_tests_get_a_verdict(database, tmp_path):
    plain = await daily_test(tmp_path, 12, "de", daily=False)
    nowhere = await daily_test(tmp_path, 12, None)  # no requested country: no verdict
    edge = await daily_test(tmp_path, verdicts.MIN_IPS, "es")
    below = await daily_test(tmp_path, verdicts.MIN_IPS - 1, "it")
    results = ingest_many([plain, nowhere, edge, below], database)
    assert all(result["imported"] for result in results)
    product = product_id(database)
    assert sorted(rows(database)) == [
        (product, "ES", True, verdicts.MIN_IPS, True),
        (product, "IT", True, verdicts.MIN_IPS - 1, False),
    ]


async def test_each_network_keeps_its_own_verdicts(database, tmp_path):
    first = await daily_test(tmp_path, 12, "de", provider="fixture")
    second = await daily_test(tmp_path, 3, "de", provider="other")
    ingest_many([first, second], database)
    found = {(p, counts) for p, _, _, _, counts in rows(database)}
    assert found == {(product_id(database), True), (product_id(database, "other"), False)}


def test_the_migrations_repeat_the_rule():
    schema = Path(storage.__file__).with_name("schema.sql").read_text()
    first = Path(storage.__file__).with_name("migrate_v15_v16.sql").read_text()
    assert f"GENERATED ALWAYS AS (unique_ips >= {verdicts.MIN_IPS}) STORED" in first
    assert " counts boolean NOT NULL,\n" in schema  # set by RECORD since v19
    assert f"end_reason IN ('{verdicts.END_REASON}')" in schema
    two_days = Path(storage.__file__).with_name("migrate_v18_v19.sql").read_text()
    assert f"v.unique_ips >= {verdicts.MIN_IPS}" in two_days
    assert f"h.previous >= {verdicts.MIN_IPS} AND h.recent >= {verdicts.BIG}" in two_days
    assert f"RANGE BETWEEN interval '{verdicts.RECENT}' PRECEDING" in two_days


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
    query = "SELECT unique_ips,counts FROM country_verdicts ORDER BY decided_at"
    assert rows(database, query) == [(60, True), (6, True), (4, False)]


async def test_a_verdict_outlives_its_run(database, tmp_path):
    """No foreign key ties a verdict to its run: the archive purge deletes old runs
    (archive/purge.py) and the verdict still holds until the next daily test."""
    ingest(await daily_test(tmp_path, 12), database)
    keys = (
        "SELECT confrelid::regclass::text FROM pg_constraint "
        "WHERE conrelid='country_verdicts'::regclass AND contype='f'"
    )
    assert rows(database, keys) == [("products",)]


def test_a_requested_countrys_status_names_low_availability_and_no_exits():
    # LOWAVAIL-1: too_few_ips is "low availability"; no_exits is the same with no IP found.
    assert verdicts.status([(False, 0)]) == {"status": "no_exits", "unique_ips": 0}
    assert verdicts.status([(False, 0), (False, 3)]) == {"status": "too_few_ips", "unique_ips": 3}
    assert verdicts.status([(True, 0), (False, 4)]) == {"status": "counts", "unique_ips": 0}
    assert verdicts.status([]) == {"status": "no_daily_test", "unique_ips": None}
