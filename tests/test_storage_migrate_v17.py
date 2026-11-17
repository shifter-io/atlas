"""In-place v16 → v17 migration (the nodes' host pressure columns, PULSE-STATE-2), the catalog
compared with a fresh v17, every stored node row kept, and a v16 build refusing v17.

fixtures/schema_v16.sql is the exact v16 schema (storage.V16_CHECKSUM); load_v16 builds the v16
release's database from test_storage_migrate_v16's v15 database the way the v16 release migrated
it, with one node row.
"""

import hashlib
from pathlib import Path

import pytest
from migration_cases import SOURCE, catalog
from test_storage_migrate_v13 import rows
from test_storage_migrate_v16 import load_v15

from atlas import storage
from atlas.calculated import store
from atlas.storage import SCHEMA_VERSION, V16_CHECKSUM, connect, initialize, migrate

pytest_plugins = ["test_storage"]

SCHEMA_V16 = Path(__file__).parent / "fixtures/schema_v16.sql"
NODE = (
    "INSERT INTO runner_regions(region_id,slug,pattern) VALUES (2,'fra1','.*fra1.*') "
    "ON CONFLICT DO NOTHING;"
    "INSERT INTO providers VALUES ('nodemaven') ON CONFLICT DO NOTHING;"
    "INSERT INTO nodes(region_id,provider,product,token_hash,status,rss_mb) "
    "SELECT region_id,'nodemaven','res','h','online',250 FROM runner_regions WHERE slug='fra1'"
)


def load_v16(database):
    """The v16 release's database (its exact schema) with one node."""
    load_v15(database)
    schema = SCHEMA_V16.read_text()
    assert hashlib.sha256(schema.encode()).hexdigest() == V16_CHECKSUM
    with connect(database) as conn:
        conn.execute((SOURCE / "migrate_v15_v16.sql").read_text())
        conn.execute(schema)
        conn.execute("UPDATE schema_versions SET version=16,checksum=%s", (V16_CHECKSUM,))
        conn.execute(NODE)


def test_v16_database_gains_the_nodes_host_pressure_columns(databases):
    database = databases()
    load_v16(database)
    nodes = rows(database, "SELECT * FROM nodes ORDER BY node_id")
    with pytest.raises(ValueError, match="db-migrate"):
        initialize(database)
    result = migrate(database)
    assert (result["from_schema_version"], result["schema_version"]) == (16, SCHEMA_VERSION)
    assert result["verified"] == [] and migrate(database)["migrated"] is False
    initialize(database)
    store.initialize(database)
    assert rows(database, "SELECT * FROM nodes ORDER BY node_id") == [
        (*node, None, None, None) for node in nodes
    ]
    reference = databases()
    store.initialize(reference)
    assert catalog(database) == catalog(reference)


def test_v17_database_is_refused_by_a_v16_build(databases, monkeypatch):
    """The v16 release's guards (its version, schema and migratable list) refuse a v17 database."""
    database = databases()
    load_v16(database)
    migrate(database)
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 16)
    monkeypatch.setattr(storage, "_schema", lambda: (SCHEMA_V16.read_text(), V16_CHECKSUM))
    monkeypatch.delitem(storage.MIGRATABLE, (16, V16_CHECKSUM))
    with pytest.raises(ValueError, match="rebuild"):
        initialize(database)
    with pytest.raises(ValueError, match="unmodified"):
        migrate(database)
