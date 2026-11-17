"""In-place v21 → v22 migration (test_batches.paused_network, VERIFY-PAUSED-BATCH-1): every stored
batch kept with the new column NULL, the column refused by v21 and written by v22, the catalog
compared with a fresh v22, and a v21 build refusing v22.

fixtures/schema_v21.sql is the exact v21 schema (storage.V21_CHECKSUM); load_v21 builds the v21
release's database from test_storage_migrate_v21's v20 database the way the v21 release migrated
it.
"""

import hashlib
from pathlib import Path

import psycopg
import pytest
from migration_cases import SOURCE, catalog
from test_storage_migrate_v13 import rows
from test_storage_migrate_v21 import load_v20

from atlas import storage
from atlas.calculated import store
from atlas.storage import SCHEMA_VERSION, V21_CHECKSUM, connect, initialize, migrate

pytest_plugins = ["test_storage"]

SCHEMA_V21 = Path(__file__).parent / "fixtures/schema_v21.sql"
BATCHES = "SELECT * FROM test_batches ORDER BY batch_id"
MARK = (
    "UPDATE test_batches SET paused_network='example' "
    "WHERE batch_id=(SELECT min(batch_id) FROM test_batches)"
)


def load_v21(database):
    """The v21 release's database (its exact schema), with a census batch stored."""
    load_v20(database)
    schema = SCHEMA_V21.read_text()
    assert hashlib.sha256(schema.encode()).hexdigest() == V21_CHECKSUM
    with connect(database) as conn:
        conn.execute((SOURCE / "migrate_v20_v21.sql").read_text())
        conn.execute(schema)
        conn.execute("UPDATE schema_versions SET version=21,checksum=%s", (V21_CHECKSUM,))
        conn.execute(
            "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,"
            "start_at,status) SELECT 'census',country_code,region_id,(SELECT min(target_id) "
            "FROM targets),'2026-11-16 06:05Z','2026-11-16 06:05Z','done' FROM countries "
            "WHERE region_id IS NOT NULL "
            "ORDER BY country_code LIMIT 1 ON CONFLICT DO NOTHING"
        )


def test_v21_database_adds_the_paused_network_of_a_batch(databases):
    database = databases()
    load_v21(database)
    batches = rows(database, BATCHES)
    assert batches
    with connect(database) as conn, pytest.raises(psycopg.errors.UndefinedColumn):
        conn.execute(MARK)
    with pytest.raises(ValueError, match="db-migrate"):
        initialize(database)
    result = migrate(database)
    assert (result["from_schema_version"], result["schema_version"]) == (21, SCHEMA_VERSION)
    assert result["verified"] == [] and migrate(database)["migrated"] is False
    initialize(database)
    store.initialize(database)
    assert rows(database, BATCHES) == [(*row, None) for row in batches]
    with connect(database) as conn:
        assert conn.execute(MARK).rowcount == 1
    reference = databases()
    store.initialize(reference)
    assert catalog(database) == catalog(reference)


def test_v22_database_is_refused_by_a_v21_build(databases, monkeypatch):
    """The v21 release's guards (its version, schema and migratable list) refuse a v22 database."""
    database = databases()
    load_v21(database)
    migrate(database)
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 21)
    monkeypatch.setattr(storage, "_schema", lambda: (SCHEMA_V21.read_text(), V21_CHECKSUM))
    monkeypatch.delitem(storage.MIGRATABLE, (21, V21_CHECKSUM))
    with pytest.raises(ValueError, match="rebuild"):
        initialize(database)
    with pytest.raises(ValueError, match="unmodified"):
        migrate(database)
