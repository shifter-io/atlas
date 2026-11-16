"""In-place v19 → v20 migration (COUNTRY-MIN-IPS-1, #1253): country_verdicts.counts is dropped and
every reader decides it from the stored unique IPs with the methodology's country_min_ips, so
every stored verdict is kept as it was and a changed minimum applies to all of them; the catalog
is compared with a fresh v20, and a v19 build refuses v20.

fixtures/schema_v19.sql is the exact v19 schema (storage.V19_CHECKSUM); load_v19 builds the v19
release's database from test_storage_migrate_v19's v18 database the way the v19 release migrated
it.
"""

import hashlib
from datetime import timedelta
from pathlib import Path

import pytest
from migration_cases import SOURCE, catalog
from test_storage_migrate_v13 import rows
from test_storage_migrate_v19 import DAY, load_v18

from atlas import storage, verdicts
from atlas.calculated import store
from atlas.storage import SCHEMA_VERSION, V19_CHECKSUM, connect, initialize, migrate

pytest_plugins = ["test_storage"]

SCHEMA_V19 = Path(__file__).parent / "fixtures/schema_v19.sql"
# (country, unique IPs of its daily tests a day apart, counts as v19 stored it under 10)
STORED = {"DE": ([900, 6, 4], [True, True, False]), "TV": ([3, 12, 2], [False, True, False])}
VERDICTS = "SELECT country_code,decided_at,run_key,unique_ips FROM country_verdicts ORDER BY 3"


def load_v19(database):
    """The v19 release's database (its exact schema)."""
    load_v18(database)
    schema = SCHEMA_V19.read_text()
    assert hashlib.sha256(schema.encode()).hexdigest() == V19_CHECKSUM
    with connect(database) as conn:
        conn.execute((SOURCE / "migrate_v18_v19.sql").read_text())
        conn.execute(schema)
        conn.execute("UPDATE schema_versions SET version=19,checksum=%s", (V19_CHECKSUM,))


def add_verdicts(database):
    """STORED as v19 verdicts, each with the counts v19 stored."""
    with connect(database) as conn:
        conn.execute("INSERT INTO providers VALUES ('minips') ON CONFLICT DO NOTHING")
        product = conn.execute(
            "INSERT INTO products(provider,name) VALUES ('minips','residential') "
            "RETURNING product_id"
        ).fetchone()[0]
        key = 8_000_000
        for country, (ips, counts) in STORED.items():
            for day, (found, counted) in enumerate(zip(ips, counts, strict=True)):
                key += 1
                conn.execute(
                    "INSERT INTO country_verdicts(product_id,country_code,decided_at,run_key,"
                    "unique_ips,counts) VALUES (%s,%s,%s,%s,%s,%s)",
                    (product, country, DAY + timedelta(days=day), key, found, counted),
                )
    return product


def counted(database, product, rule):
    """{country: [whether each verdict counts under the minimum `rule`]}, read as v20 reads."""
    query = f"SELECT country_code,counts FROM ({verdicts.ruled('<%(until)s')}) r "
    query += "WHERE product_id=%(product)s ORDER BY 1,decided_at"
    found = {}
    with connect(database) as conn:
        params = {"until": "infinity", "min_ips": rule, "product": product}
        for country, counts in conn.execute(query, params):
            found.setdefault(country.strip(), []).append(counts)
    return found


def test_v19_verdicts_are_kept_and_read_under_any_minimum(databases):
    database = databases()
    load_v19(database)
    product = add_verdicts(database)
    before = rows(database, VERDICTS)
    with pytest.raises(ValueError, match="db-migrate"):
        initialize(database)
    result = migrate(database)
    assert (result["from_schema_version"], result["schema_version"]) == (19, SCHEMA_VERSION)
    assert result["verified"] == [] and migrate(database)["migrated"] is False
    initialize(database)
    store.initialize(database)
    assert rows(database, VERDICTS) == before  # no verdict rewritten
    # Under v19's minimum of 10 every verdict counts as v19 stored it; under 2 the small ones too.
    assert counted(database, product, 10) == {c: counts for c, (_, counts) in STORED.items()}
    assert counted(database, product, 2) == {"DE": [True] * 3, "TV": [True] * 3}
    reference = databases()
    store.initialize(reference)
    assert catalog(database) == catalog(reference)


def test_v20_database_is_refused_by_a_v19_build(databases, monkeypatch):
    """The v19 release's guards (its version, schema and migratable list) refuse a v20 database."""
    database = databases()
    load_v19(database)
    migrate(database)
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 19)
    monkeypatch.setattr(storage, "_schema", lambda: (SCHEMA_V19.read_text(), V19_CHECKSUM))
    monkeypatch.delitem(storage.MIGRATABLE, (19, V19_CHECKSUM))
    with pytest.raises(ValueError, match="rebuild"):
        initialize(database)
    with pytest.raises(ValueError, match="unmodified"):
        migrate(database)
