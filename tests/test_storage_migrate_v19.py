"""In-place v18 → v19 migration (country_verdicts.counts by the two-days-in-a-row rule, LOWAVAIL-1):
every stored verdict set again from the ones before it as an import sets it, the calculation's
verdict setting forgotten so its memos are counted again, the catalog compared with a fresh v19,
and a v18 build refusing v19.

fixtures/schema_v18.sql is the exact v18 schema (storage.V18_CHECKSUM); load_v18 builds the v18
release's database from test_storage_migrate_v18's v17 database the way the v18 release migrated
it.
"""

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from migration_cases import SOURCE, catalog
from test_storage_migrate_v13 import rows
from test_storage_migrate_v18 import load_v17

from atlas import storage, verdicts
from atlas.calculated import store
from atlas.storage import SCHEMA_VERSION, V18_CHECKSUM, connect, initialize, migrate

pytest_plugins = ["test_storage"]

SCHEMA_V18 = Path(__file__).parent / "fixtures/schema_v18.sql"
DAY = datetime(2026, 11, 14, 18, tzinfo=UTC)
# (country, [unique IPs of its daily tests, a day apart]) and the counts each gets in v19
HISTORIES = {
    # big, a bad day that counts, a second that does not; back to 30, and 8 counts again: its 900
    # of four days before is in the 7 days.
    "DE": ([900, 6, 4, 30, 8], [True, True, False, True, True]),
    "TV": ([3, 12, 2], [False, True, False]),  # never big: under 10 never counts
    "GB": ([60, 9], [True, True]),
    "FR": ([40, 9], [True, False]),  # 10 or more before, but never 50
}
# CV: 900, then 30 a day for nine days, then 8: the 900 is ten days before, outside the 7 days
HISTORIES["CV"] = ([900, *[30] * 9, 8], [True] * 10 + [False])


def load_v18(database):
    """The v18 release's database (its exact schema)."""
    load_v17(database)
    schema = SCHEMA_V18.read_text()
    assert hashlib.sha256(schema.encode()).hexdigest() == V18_CHECKSUM
    with connect(database) as conn:
        conn.execute((SOURCE / "migrate_v17_v18.sql").read_text())
        conn.execute(schema)
        conn.execute("UPDATE schema_versions SET version=18,checksum=%s", (V18_CHECKSUM,))


def add_verdicts(database):
    """HISTORIES as v18 verdicts (counts generated from each verdict's own IPs)."""
    with connect(database) as conn:
        conn.execute("INSERT INTO providers VALUES ('lowavail') ON CONFLICT DO NOTHING")
        product = conn.execute(
            "INSERT INTO products(provider,name) VALUES ('lowavail','residential') "
            "RETURNING product_id"
        ).fetchone()[0]
        key = 9_000_000
        for country, (ips, _) in HISTORIES.items():
            for day, found in enumerate(ips):
                key += 1
                conn.execute(
                    "INSERT INTO country_verdicts(product_id,country_code,decided_at,run_key,"
                    "unique_ips) VALUES (%s,%s,%s,%s,%s)",
                    (product, country, DAY + timedelta(days=day), key, found),
                )
    return product


def test_v18_verdicts_count_by_the_two_days_rule(databases):
    database = databases()
    load_v18(database)
    product = add_verdicts(database)
    with pytest.raises(ValueError, match="db-migrate"):
        initialize(database)
    result = migrate(database)
    assert (result["from_schema_version"], result["schema_version"]) == (18, SCHEMA_VERSION)
    assert result["verified"] == [] and migrate(database)["migrated"] is False
    initialize(database)
    store.initialize(database)
    # Since v20 no column stores it: read under #1137's minimum of 10, the one v19 stored.
    found = {}
    query = f"SELECT country_code,counts FROM ({verdicts.ruled('<%(until)s')}) r "
    query += "WHERE product_id=%(product)s ORDER BY 1,decided_at"
    params = {"until": "infinity", "min_ips": 10, "product": product}
    with connect(database) as conn:
        for country, counts in conn.execute(query, params):
            found.setdefault(country, []).append(counts)
    assert found == {country: counts for country, (_, counts) in HISTORIES.items()}
    reference = databases()
    store.initialize(reference)
    assert catalog(database) == catalog(reference)


def test_the_migration_forgets_the_calculations_verdict_setting(databases):
    database = databases()
    initialize(database)
    store.initialize(database)
    step = (SOURCE / "migrate_v18_v19.sql").read_text()
    with connect(database) as conn:
        for marker in ("fleet-country-verdicts", "rollups-country-verdicts", "kept"):
            conn.execute(
                "INSERT INTO calculated_distinct(digest,counts) VALUES (%s,'{\"on\": true}')",
                (marker,),
            )
        conn.execute(step[step.index("DO $$") :])  # its last statement
    assert rows(database, "SELECT digest FROM calculated_distinct") == [("kept",)]


def test_v19_database_is_refused_by_a_v18_build(databases, monkeypatch):
    """The v18 release's guards (its version, schema and migratable list) refuse a v19 database."""
    database = databases()
    load_v18(database)
    migrate(database)
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 18)
    monkeypatch.setattr(storage, "_schema", lambda: (SCHEMA_V18.read_text(), V18_CHECKSUM))
    monkeypatch.delitem(storage.MIGRATABLE, (18, V18_CHECKSUM))
    with pytest.raises(ValueError, match="rebuild"):
        initialize(database)
    with pytest.raises(ValueError, match="unmodified"):
        migrate(database)
