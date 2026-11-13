"""In-place v13 → v14 migration (scope_ips, each scope's IP membership set, filled from the
stored observations), the catalog compared with a fresh v14, and a v13 build refusing v14.

fixtures/schema_v13.sql is the exact v13 schema (storage.V13_CHECKSUM); load_v13 builds the v13
release's database from test_storage_migrate_v13's v12 database the way the v13 release migrated
it. Its observations repeat over a monthly and two daily partitions (test_migration_v12), so a
scope's IP is stored more than once.
"""

import hashlib
from pathlib import Path

import pytest
from migration_cases import SOURCE, catalog
from test_scope_ips import MEMBERS, RECOUNT
from test_storage_migrate_v13 import load_v12, rows

from atlas import storage
from atlas.calculated import store
from atlas.storage import SCHEMA_VERSION, V13_CHECKSUM, connect, initialize, migrate

pytest_plugins = ["test_storage"]

SCHEMA_V13 = Path(__file__).parent / "fixtures/schema_v13.sql"


def load_v13(database):
    """The live shape: the v13 release's database (its exact schema), with its event buckets."""
    load_v12(database)
    schema = SCHEMA_V13.read_text()
    assert hashlib.sha256(schema.encode()).hexdigest() == V13_CHECKSUM
    with connect(database) as conn:
        conn.execute((SOURCE / "migrate_v12_v13.sql").read_text())
        conn.execute(schema)
        conn.execute("UPDATE schema_versions SET version=13,checksum=%s", (V13_CHECKSUM,))


def test_v13_database_fills_scope_ips_from_its_observations(databases):
    database = databases()
    load_v13(database)
    observations = rows(database, "SELECT count(*) FROM ip_observations")[0][0]
    with pytest.raises(ValueError, match="db-migrate"):
        initialize(database)
    result = migrate(database)
    assert (result["from_schema_version"], result["schema_version"]) == (13, SCHEMA_VERSION)
    assert result["verified"] == [] and migrate(database)["migrated"] is False
    initialize(database)
    store.initialize(database)
    members = rows(database, MEMBERS)
    assert members == rows(database, RECOUNT) and 0 < len(members) < observations
    reference = databases()
    store.initialize(reference)
    assert catalog(database) == catalog(reference)


def test_v14_database_is_refused_by_a_v13_build(databases, monkeypatch):
    """The v13 release's guards (its version, schema and migratable list) refuse a v14 database."""
    database = databases()
    load_v13(database)
    migrate(database)
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 13)
    monkeypatch.setattr(storage, "_schema", lambda: (SCHEMA_V13.read_text(), V13_CHECKSUM))
    monkeypatch.delitem(storage.MIGRATABLE, (13, V13_CHECKSUM))
    with pytest.raises(ValueError, match="rebuild"):
        initialize(database)
    with pytest.raises(ValueError, match="unmodified"):
        migrate(database)
