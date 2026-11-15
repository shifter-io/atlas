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


def test_the_schema_repeats_the_minimum():
    schema = Path(storage.__file__).with_name("schema.sql").read_text()
    migration = Path(storage.__file__).with_name("migrate_v15_v16.sql").read_text()
    rule = f"GENERATED ALWAYS AS (unique_ips >= {verdicts.MIN_IPS}) STORED"
    assert rule in schema and rule in migration
    assert f"end_reason IN ('{verdicts.END_REASON}')" in schema


async def test_a_verdict_outlives_its_run(database, tmp_path):
    """No foreign key ties a verdict to its run: the archive purge deletes old runs
    (archive/purge.py) and the verdict still holds until the next daily test."""
    ingest(await daily_test(tmp_path, 12), database)
    keys = (
        "SELECT confrelid::regclass::text FROM pg_constraint "
        "WHERE conrelid='country_verdicts'::regclass AND contype='f'"
    )
    assert rows(database, keys) == [("products",)]
