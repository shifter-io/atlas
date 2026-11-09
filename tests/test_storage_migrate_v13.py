"""In-place v12 → v13 migration (test_event_buckets, kept by a statement trigger on test_events and
filled from the stored events), the trigger on a multi-row insert, a COPY and a later bucket, and a
v12 build refusing v13.

fixtures/schema_v12.sql is the exact v12 schema (storage.V12_CHECKSUM); load_v12 builds the v12
release's database from test_migration_v12's v11 database the way the v12 release migrated it,
with events over two buckets and two products (one product-less).
"""

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from migration_cases import SOURCE, catalog
from test_migration_v12 import load_v11

from proxy_index import storage
from proxy_index.calculated import store
from proxy_index.partitions import partition
from proxy_index.storage import SCHEMA_VERSION, V12_CHECKSUM, connect, initialize, migrate

pytest_plugins = ["test_storage"]

SCHEMA_V12 = Path(__file__).parent / "fixtures/schema_v12.sql"
AT = datetime(2026, 11, 3, 10, 5, tzinfo=UTC)
# The buckets as test_events counts them, and as the table holds them.
CATEGORY = (
    "CASE WHEN kind LIKE 'pulse%%' THEN 'pulse' WHEN kind='daily_test_finished' THEN 'census' "
    "WHEN kind='ip_overlap' THEN 'audit' ELSE 'incident' END"
)
RECOUNT = (
    f"SELECT date_bin('30 minutes',at,'2000-01-01 00:00+00'),coalesce(product_id,0),{CATEGORY},"
    "count(*)::int,(count(*) FILTER (WHERE signal>0))::int,"
    "(count(*) FILTER (WHERE signal<0))::int,max(at) FROM test_events GROUP BY 1,2,3 ORDER BY 1,2,3"
)
BUCKETS = "SELECT * FROM test_event_buckets ORDER BY 1,2,3"
TRIGGERS = (
    "SELECT tgname,pg_get_triggerdef(oid) FROM pg_trigger "
    "WHERE tgrelid='test_events'::regclass AND NOT tgisinternal ORDER BY 1"
)
EVENTS = (
    "INSERT INTO test_events(at,kind,product_id,signal) SELECT %(at)s+n*interval '1 minute',"
    "(ARRAY['pulse_finished','pulse_down','daily_test_finished','recovered','ip_overlap'])[n%%5+1],"
    "CASE WHEN n%%4=0 THEN NULL ELSE %(product)s END,(n%%3)-1 FROM generate_series(0,%(n)s-1) n"
)


def product(conn, provider="fixture"):
    """A product id of provider (made when missing)."""
    conn.execute("INSERT INTO providers VALUES (%s) ON CONFLICT DO NOTHING", (provider,))
    conn.execute(
        "INSERT INTO products(provider,name) VALUES (%s,'residential') ON CONFLICT DO NOTHING",
        (provider,),
    )
    query = "SELECT product_id FROM products WHERE provider=%s AND name='residential'"
    return conn.execute(query, (provider,)).fetchone()[0]


def load_v12(database):
    """The live shape: the v12 release's database (its exact schema), with 50 events over two
    30-minute buckets (10:05 to 10:54) in five kinds of four tabs, a quarter without a product."""
    load_v11(database)
    schema = SCHEMA_V12.read_text()
    assert hashlib.sha256(schema.encode()).hexdigest() == V12_CHECKSUM
    with connect(database) as conn:
        conn.execute((SOURCE / "migrate_v11_v12.sql").read_text())
        conn.execute(schema)
        conn.execute("UPDATE schema_versions SET version=12,checksum=%s", (V12_CHECKSUM,))
        partition(conn, "test_events", AT)
        conn.execute(EVENTS, {"at": AT, "product": product(conn), "n": 50})


def rows(database, query):
    with connect(database) as conn:
        return conn.execute(query).fetchall()


def test_v12_database_fills_the_event_buckets_from_its_events(databases):
    database = databases()
    load_v12(database)
    with pytest.raises(ValueError, match="db-migrate"):
        initialize(database)
    result = migrate(database)
    assert (result["from_schema_version"], result["schema_version"]) == (12, SCHEMA_VERSION)
    assert result["verified"] == [] and migrate(database)["migrated"] is False
    initialize(database)
    store.initialize(database)
    buckets = rows(database, BUCKETS)
    assert buckets == rows(database, RECOUNT)
    assert rows(database, "SELECT * FROM test_event_bucket_counts ORDER BY 1,2,3") == buckets
    assert len({b[0] for b in buckets}) == 2 and {b[2] for b in buckets} == {
        "pulse",
        "census",
        "incident",
        "audit",
    }
    assert sum(b[3] for b in buckets) == rows(database, "SELECT count(*) FROM test_events")[0][0]
    assert sum(b[4] + b[5] for b in buckets) == 33 and 0 in {b[1] for b in buckets}
    reference = databases()
    store.initialize(reference)
    assert catalog(database) == catalog(reference)
    assert rows(database, TRIGGERS) == rows(reference, TRIGGERS) != []
    with connect(database) as conn:  # the trigger keeps the backfilled buckets
        conn.execute(EVENTS, {"at": AT, "product": product(conn), "n": 5})
    assert rows(database, BUCKETS) == rows(database, RECOUNT)


def test_the_trigger_counts_inserts_copies_and_later_buckets(database):
    """A multi-row insert, a COPY into the parent and an insert in a later bucket (another month's
    partition) each add their rows to the right buckets, once per statement."""
    with connect(database) as conn:
        later = AT + timedelta(days=30)
        for moment in (AT, later):
            partition(conn, "test_events", moment)
        ids = product(conn), product(conn, "other")
        conn.execute(EVENTS, {"at": AT, "product": ids[0], "n": 20})
        with conn.cursor().copy("COPY test_events(at,kind,product_id,signal) FROM STDIN") as copy:
            for n in range(6):
                copy.write_row((AT + timedelta(minutes=n), "pulse_finished", ids[1], 1))
        before = conn.execute(BUCKETS).fetchall()
        conn.execute(
            "INSERT INTO test_events(at,kind,product_id,signal,severity) VALUES "
            "(%s,'latency_rise',%s,-1,'minor'),(%s,'latency_rise',%s,-1,'minor')",
            (later, ids[1], later + timedelta(minutes=24), ids[1]),
        )
        after = conn.execute(BUCKETS).fetchall()
    assert before == rows(database, RECOUNT)[: len(before)]
    assert (AT.replace(minute=0), ids[1], "pulse", 6, 6, 0, AT + timedelta(minutes=5)) in before
    assert after == before + [
        (later.replace(minute=0), ids[1], "incident", 2, 0, 2, later + timedelta(minutes=24))
    ]
    assert after == rows(database, RECOUNT)


def test_v13_database_is_refused_by_a_v12_build(databases, monkeypatch):
    """The v12 release's guards (its version, schema and migratable list) refuse a v13 database."""
    database = databases()
    load_v12(database)
    migrate(database)
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 12)
    monkeypatch.setattr(storage, "_schema", lambda: (SCHEMA_V12.read_text(), V12_CHECKSUM))
    monkeypatch.delitem(storage.MIGRATABLE, (12, V12_CHECKSUM))
    with pytest.raises(ValueError, match="rebuild"):
        initialize(database)
    with pytest.raises(ValueError, match="unmodified"):
        migrate(database)
