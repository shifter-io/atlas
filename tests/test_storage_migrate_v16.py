"""In-place v15 → v16 migration (country_verdicts, filled from the stored daily tests, and
outages.end_reason), the catalog compared with a fresh v16, the fill equal to what imports
write, and a v15 build refusing v16.

fixtures/schema_v15.sql is the exact v15 schema (storage.V15_CHECKSUM); load_v15 builds the v15
release's database from test_storage_migrate_v15's v14 database the way the v15 release migrated
it, three of its runs then marked as daily tests of 0, 7 and 14 unique IPs.
"""

import hashlib
from pathlib import Path

import pytest
from migration_cases import SOURCE, catalog
from test_storage_migrate_v13 import rows
from test_storage_migrate_v15 import load_v14

from atlas import storage, verdicts
from atlas.calculated import store
from atlas.storage import SCHEMA_VERSION, V15_CHECKSUM, connect, initialize, migrate

pytest_plugins = ["test_storage"]

SCHEMA_V15 = Path(__file__).parent / "fixtures/schema_v15.sql"
# Three runs that started at different times, marked as daily tests of DE with 0, 7 and 14 IPs.
DAILY = "SELECT DISTINCT ON (started_at) run_key FROM test_runs ORDER BY started_at LIMIT 3"
MARK = (
    "UPDATE test_runs SET summary=summary||jsonb_build_object('test_kind','daily',"
    "'unique_ips',%s::int) WHERE run_key=%s"
)
COUNTRY = (
    "UPDATE pool_stats SET requested_country='de' WHERE pool_id IN (SELECT s.pool_id "
    "FROM scope_stats s JOIN test_runs t USING(scope_id) WHERE t.run_key=ANY(%s))"
)
VERDICTS = (
    "SELECT v.product_id=s.product_id,v.country_code,v.decided_at=t.started_at,v.run_key,"
    "v.unique_ips,v.counts FROM country_verdicts v JOIN test_runs t USING(run_key) "
    "JOIN scope_stats s USING(scope_id) ORDER BY v.run_key"
)


def load_v15(database):
    """The v15 release's database (its exact schema), three of its runs daily tests."""
    load_v14(database)
    schema = SCHEMA_V15.read_text()
    assert hashlib.sha256(schema.encode()).hexdigest() == V15_CHECKSUM
    with connect(database) as conn:
        conn.execute((SOURCE / "migrate_v14_v15.sql").read_text())
        conn.execute(schema)
        conn.execute("UPDATE schema_versions SET version=15,checksum=%s", (V15_CHECKSUM,))
        keys = [key for (key,) in conn.execute(DAILY).fetchall()]
        assert len(keys) == 3
        for found, key in zip((0, 7, 14), keys, strict=True):
            conn.execute(MARK, (found, key))
        conn.execute(COUNTRY, (keys,))
    return keys


def recorded(database):
    """The verdicts an import writes for every stored run (verdicts.RECORD), in place of the
    stored ones; the stored ones are restored by the rollback."""
    with connect(database) as conn:
        conn.execute("DELETE FROM country_verdicts")
        conn.execute("CREATE TEMP TABLE incoming_runs AS SELECT run_key FROM test_runs")
        conn.execute(verdicts.RECORD)
        found = conn.execute(VERDICTS).fetchall()
        conn.rollback()
    return found


def test_v15_database_fills_country_verdicts_from_its_daily_tests(databases):
    database = databases()
    keys = load_v15(database)
    outages = rows(database, "SELECT * FROM outages ORDER BY outage_id")
    with pytest.raises(ValueError, match="db-migrate"):
        initialize(database)
    result = migrate(database)
    assert (result["from_schema_version"], result["schema_version"]) == (15, SCHEMA_VERSION)
    assert result["verified"] == [] and migrate(database)["migrated"] is False
    initialize(database)
    store.initialize(database)
    found = rows(database, VERDICTS)
    assert found == [
        (True, "DE", True, key, ips, ips >= verdicts.MIN_IPS)
        for key, ips in zip(keys, (0, 7, 14), strict=True)
    ]
    assert found == recorded(database)
    assert rows(database, "SELECT * FROM outages ORDER BY outage_id") == [
        (*outage, None) for outage in outages
    ]
    reference = databases()
    store.initialize(reference)
    assert catalog(database) == catalog(reference)


def test_v16_database_is_refused_by_a_v15_build(databases, monkeypatch):
    """The v15 release's guards (its version, schema and migratable list) refuse a v16 database."""
    database = databases()
    load_v15(database)
    migrate(database)
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 15)
    monkeypatch.setattr(storage, "_schema", lambda: (SCHEMA_V15.read_text(), V15_CHECKSUM))
    monkeypatch.delitem(storage.MIGRATABLE, (15, V15_CHECKSUM))
    with pytest.raises(ValueError, match="rebuild"):
        initialize(database)
    with pytest.raises(ValueError, match="unmodified"):
        migrate(database)
