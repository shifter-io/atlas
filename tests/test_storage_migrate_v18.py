"""In-place v17 → v18 migration (batch_runs.start_at, a moved run's own start, PULSE-STATE-2),
the catalog compared with a fresh v18, every stored run kept with no start of its own, and a v17
build refusing v18.

fixtures/schema_v17.sql is the exact v17 schema (storage.V17_CHECKSUM); load_v17 builds the v17
release's database from test_storage_migrate_v17's v16 database the way the v17 release migrated
it.
"""

import hashlib
from pathlib import Path

import pytest
from migration_cases import SOURCE, catalog
from test_storage_migrate_v13 import rows
from test_storage_migrate_v17 import load_v16

from atlas import storage
from atlas.calculated import store
from atlas.storage import SCHEMA_VERSION, V17_CHECKSUM, connect, initialize, migrate

pytest_plugins = ["test_storage"]

SCHEMA_V17 = Path(__file__).parent / "fixtures/schema_v17.sql"


def load_v17(database):
    """The v17 release's database (its exact schema)."""
    load_v16(database)
    schema = SCHEMA_V17.read_text()
    assert hashlib.sha256(schema.encode()).hexdigest() == V17_CHECKSUM
    with connect(database) as conn:
        conn.execute((SOURCE / "migrate_v16_v17.sql").read_text())
        conn.execute(schema)
        conn.execute("UPDATE schema_versions SET version=17,checksum=%s", (V17_CHECKSUM,))


def test_v17_database_gains_the_runs_own_start(databases):
    database = databases()
    load_v17(database)
    runs = rows(database, "SELECT * FROM batch_runs ORDER BY batch_id,product_id")
    with pytest.raises(ValueError, match="db-migrate"):
        initialize(database)
    result = migrate(database)
    assert (result["from_schema_version"], result["schema_version"]) == (17, SCHEMA_VERSION)
    assert result["verified"] == [] and migrate(database)["migrated"] is False
    initialize(database)
    store.initialize(database)
    assert rows(database, "SELECT * FROM batch_runs ORDER BY batch_id,product_id") == [
        (*run, None) for run in runs
    ]
    reference = databases()
    store.initialize(reference)
    assert catalog(database) == catalog(reference)


def test_v18_database_is_refused_by_a_v17_build(databases, monkeypatch):
    """The v17 release's guards (its version, schema and migratable list) refuse a v18 database."""
    database = databases()
    load_v17(database)
    migrate(database)
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 17)
    monkeypatch.setattr(storage, "_schema", lambda: (SCHEMA_V17.read_text(), V17_CHECKSUM))
    monkeypatch.delitem(storage.MIGRATABLE, (17, V17_CHECKSUM))
    with pytest.raises(ValueError, match="rebuild"):
        initialize(database)
    with pytest.raises(ValueError, match="unmodified"):
        migrate(database)
