"""Loaders and helpers of the in-place migration tests: a v4 database from a pg_dump, then each
release's database built from it the way that release migrated it.

fixtures/database_v4.sql.gz was produced by proxy-index 0.8.0 (schema v4, calculated v1): two
benchmarked providers with enrichment and evidence, a third with a Shifter-style provider_config,
float timeout, rate, custom URL, an unknown config key and a non-canonical bucket label; three
builds (ready and ranked, then expired and retained scores, then an unpublished draft).
fixtures/schema_v5.sql is the exact v5 schema (storage.V5_CHECKSUM) and fixtures/calculated_v2.sql
the exact calculated v2 schema (store.V2_CHECKSUM); load_v5 builds a v5 database from the same dump
the way the v5 release migrated it; fixtures/schema_v6.sql and schema_v7.sql are the v6 and v7
schemas except for one SQL comment each that the rename (#3) changed, so their own checksums are
V6_FIXTURE_SHA256 and V7_FIXTURE_SHA256, while the loaders record storage.V6_CHECKSUM and
V7_CHECKSUM as a real v6 or v7 database does; fixtures/schema_v8.sql is the exact v8 schema
(storage.V8_CHECKSUM), and fixtures/calculated_v6.sql the exact calculated v6 schema
(store.V6_CHECKSUM); fixtures/schema_v9.sql is the exact v9 schema (storage.V9_CHECKSUM).
"""

import gzip
import hashlib
import json
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from proxy_index.calculated import store
from proxy_index.calculated.api import app
from proxy_index.storage import (
    V5_CHECKSUM,
    V6_CHECKSUM,
    V7_CHECKSUM,
    V8_CHECKSUM,
    V9_CHECKSUM,
    connect,
)

FIXTURE = Path(__file__).parent / "fixtures/database_v4.sql.gz"
SCHEMA_V5 = Path(__file__).parent / "fixtures/schema_v5.sql"
SCHEMA_V6 = Path(__file__).parent / "fixtures/schema_v6.sql"
SCHEMA_V7 = Path(__file__).parent / "fixtures/schema_v7.sql"
SCHEMA_V8 = Path(__file__).parent / "fixtures/schema_v8.sql"
SCHEMA_V9 = Path(__file__).parent / "fixtures/schema_v9.sql"
# The fixtures' own SHA-256: the shipped v6 and v7 schemas except for one SQL comment that the
# rename (#3) changed. A real v6 or v7 database records storage.V6_CHECKSUM or V7_CHECKSUM, which
# the loaders below still insert into schema_versions.
V6_FIXTURE_SHA256 = "6af11b950a40157784afffcb428f4f2d9d9ce7ac9d34562644383d6150f40d02"
V7_FIXTURE_SHA256 = "4968de7d7b7bbae34518df9653e1f225ee6b9e6aa37c9c5f00ba9fd997ed0887"
CALCULATED_V2 = Path(__file__).parent / "fixtures/calculated_v2.sql"
CALCULATED_V3 = Path(__file__).parent / "fixtures/calculated_v3.sql"
CALCULATED_V4 = Path(__file__).parent / "fixtures/calculated_v4.sql"
CALCULATED_V5 = Path(__file__).parent / "fixtures/calculated_v5.sql"
CALCULATED_V6 = Path(__file__).parent / "fixtures/calculated_v6.sql"
SOURCE = Path(store.__file__).parents[1]
POLICY = Path("configs/methodology.toml")
RESOURCES = ("products", "scores", "series", "breakdowns", "overlap", "fleet", "runs")


def load_v4(database):
    with connect(database) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    with connect(database) as conn:
        conn.execute(gzip.decompress(FIXTURE.read_bytes()).decode())


def load_v5(database, *, layered=True):
    """The v4 dump after the v5 release's migration: v4 → v5 SQL, then the v5 schema."""
    load_v4(database)
    schema = SCHEMA_V5.read_text()
    calculated = CALCULATED_V2.read_text()
    assert hashlib.sha256(schema.encode()).hexdigest() == V5_CHECKSUM
    assert hashlib.sha256(calculated.encode()).hexdigest() == store.V2_CHECKSUM
    with connect(database) as conn:
        conn.execute((SOURCE / "migrate_v4_v5.sql").read_text())
        conn.execute((SOURCE / "calculated/migrate_v1_v2.sql").read_text())
        conn.execute(schema)
        conn.execute(calculated)
        conn.execute("DELETE FROM schema_versions")
        conn.execute("INSERT INTO schema_versions(version,checksum) VALUES (5,%s)", (V5_CHECKSUM,))
        conn.execute("DELETE FROM calculation_schema")
        conn.execute("INSERT INTO calculation_schema VALUES (2,%s)", (store.V2_CHECKSUM,))


def canonical(value):
    return json.dumps(value, sort_keys=True, default=str)


async def api_bodies(database, builds):
    bodies = {}
    async with TestClient(TestServer(app(database))) as client:
        paths = [f"/v1/snapshots/{b}" for b in builds] + [
            f"/v1/snapshots/{b}/{r}?window={w}"
            for b in builds
            for r in RESOURCES
            for w in ("24h", "7d", "30d")
        ]
        for path in paths:
            response = await client.get(path)
            bodies[path] = (response.status, response.headers["ETag"], await response.read())
    return bodies


def catalog(database):
    """Columns, constraints, indexes, views and functions of the non-partition objects."""
    queries = [
        "SELECT c.relname,a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull,"
        "pg_get_expr(d.adbin,d.adrelid),a.attidentity FROM pg_attribute a "
        "JOIN pg_class c ON c.oid=a.attrelid LEFT JOIN pg_attrdef d "
        "ON (d.adrelid,d.adnum)=(a.attrelid,a.attnum) WHERE c.relnamespace='public'::regnamespace "
        "AND a.attnum>0 AND NOT a.attisdropped AND c.relkind IN ('r','p','v') "
        "AND NOT c.relispartition ORDER BY 1,2",
        "SELECT c.relname,conname,pg_get_constraintdef(o.oid) FROM pg_constraint o "
        "JOIN pg_class c ON c.oid=o.conrelid WHERE c.relnamespace='public'::regnamespace "
        "AND NOT c.relispartition ORDER BY 1,2",
        "SELECT c.relname,pg_get_indexdef(i.indexrelid) FROM pg_index i "
        "JOIN pg_class c ON c.oid=i.indrelid WHERE c.relnamespace='public'::regnamespace "
        "AND NOT c.relispartition ORDER BY 1,2",
        "SELECT viewname,definition FROM pg_views WHERE schemaname='public' ORDER BY 1",
        "SELECT proname,pg_get_functiondef(oid) FROM pg_proc "
        "WHERE pronamespace='public'::regnamespace ORDER BY 1",
    ]
    with connect(database) as conn:
        return [conn.execute(query).fetchall() for query in queries]


def v5_state(database):
    with connect(database) as conn:
        return {
            "summaries": dict(
                conn.execute("SELECT run_id::text,stored_run_summary(run_key) FROM test_runs")
            ),
            "buckets": conn.execute(
                "SELECT run_key,bucket_start,run_bucket_counters(b) FROM run_buckets b ORDER BY 1,2"
            ).fetchall(),
            "scopes": dict(
                conn.execute(
                    "SELECT scope_hash,jsonb_build_array(scope,metrics) FROM scope_details"
                )
            ),
            "pools": dict(conn.execute("SELECT pool_hash,definition FROM pool_details")),
            "payloads": dict(conn.execute("SELECT build_id,payload FROM calculated_snapshots")),
        }


def load_v6_calculated_v2(database):
    """A v6 base schema whose calculated layer is still v2 (a v6 development database)."""
    load_v5(database)
    schema = SCHEMA_V6.read_text()
    assert hashlib.sha256(schema.encode()).hexdigest() == V6_FIXTURE_SHA256
    with connect(database) as conn:
        conn.execute((SOURCE / "migrate_v5_v6.sql").read_text())
        conn.execute(schema)
        conn.execute("DELETE FROM schema_versions")
        conn.execute("INSERT INTO schema_versions(version,checksum) VALUES (6,%s)", (V6_CHECKSUM,))


def load_v6(database):
    """The v6 release's database: v6 base schema and the current calculated v3 layer, with a
    census and a pulse batch."""
    load_v6_calculated_v2(database)
    with connect(database) as conn:
        conn.execute((SOURCE / "calculated/migrate_v2_v3.sql").read_text())
        conn.execute(CALCULATED_V3.read_text())
        conn.execute("DELETE FROM calculation_schema")
        conn.execute("INSERT INTO calculation_schema VALUES (%s,%s)", (3, store.V3_CHECKSUM))
    with connect(database) as conn:
        conn.execute("INSERT INTO runner_regions(region_id,slug,pattern) VALUES (1,'fra1','fra')")
        conn.execute(
            "INSERT INTO countries VALUES ('DE','Germany',1,'03:00') ON CONFLICT DO NOTHING"
        )
        for kind in ("census", "pulse"):
            conn.execute(
                "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,"
                "start_at,status) VALUES (%s,'DE',1,'generic_http',now(),now(),'done')",
                (kind,),
            )


def load_v7(database):
    """The v7 release's database: v6 plus migrate_v6_v7.sql and the v7 schema."""
    load_v6(database)
    schema = SCHEMA_V7.read_text()
    assert hashlib.sha256(schema.encode()).hexdigest() == V7_FIXTURE_SHA256
    with connect(database) as conn:
        conn.execute((SOURCE / "migrate_v6_v7.sql").read_text())
        conn.execute(schema)
        conn.execute("DELETE FROM schema_versions")
        conn.execute("INSERT INTO schema_versions(version,checksum) VALUES (7,%s)", (V7_CHECKSUM,))


def load_calculated(database, version):
    """A schema v8 database whose calculated layer is the v4, v5 or v6 release's (from v3 on v7)."""
    load_v7(database)
    fixtures = {
        4: (CALCULATED_V4, store.V4_CHECKSUM),
        5: (CALCULATED_V5, store.V5_CHECKSUM),
        6: (CALCULATED_V6, store.V6_CHECKSUM),
    }
    for path, checksum in fixtures.values():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == checksum
    with connect(database) as conn:
        conn.execute((SOURCE / "migrate_v7_v8.sql").read_text())
        conn.execute(SCHEMA_V8.read_text())
        conn.execute("DELETE FROM schema_versions")
        conn.execute("INSERT INTO schema_versions VALUES (8,%s)", (V8_CHECKSUM,))
        for n in range(4, version + 1):
            conn.execute((SOURCE / f"calculated/migrate_v{n - 1}_v{n}.sql").read_text())
            conn.execute(fixtures[n][0].read_text())
        conn.execute("DELETE FROM calculation_schema")
        conn.execute(
            "INSERT INTO calculation_schema VALUES (%s,%s)", (version, fixtures[version][1])
        )


def breakdowns_by_build(conn):
    return dict(
        conn.execute(
            "SELECT w.build_id,count(*) FROM calculated_breakdown_details "
            "JOIN calculated_windows w USING(result_id) GROUP BY 1"
        )
    )


PLACE = "jsonb_build_array(country_code,country,region_code,region,city)"
# The distinct place and ASN names of enrichment and the calculated v6 layer together, and the
# calculated places no enrichment location has (country and region rows).
V6_NAMES = (
    f"SELECT (SELECT count(DISTINCT t) FROM (SELECT {PLACE} t FROM enrichment_locations "
    f"UNION ALL SELECT {PLACE} FROM places) u),(SELECT count(DISTINCT t) FROM (SELECT "
    "jsonb_build_array(asn,asn_organization) t FROM enrichment_networks UNION ALL "
    f"SELECT jsonb_build_array(asn,name) FROM asn_labels) u),(SELECT count(*) FROM places "
    f"WHERE {PLACE} NOT IN (SELECT {PLACE} FROM enrichment_locations))"
)


def load_v8(database):
    """The live shape: the v8 release's database with the calculated v5 release's layer."""
    load_calculated(database, 5)
    with connect(database) as conn:
        # Real-shaped extras: a stored city at a second coordinate, a stored ASN with another ISP.
        assert conn.execute(
            "INSERT INTO enrichment_locations(country_code,country,region_code,region,city,"
            "latitude,longitude) SELECT country_code,country,region_code,region,city,"
            "latitude+0.1,longitude FROM enrichment_locations WHERE city IS NOT NULL "
            "ORDER BY location_id LIMIT 1 RETURNING city"
        ).fetchone()
        assert conn.execute(
            "INSERT INTO enrichment_networks(isp,organization,asn,asn_organization,connection_type,"
            "usage_type) SELECT 'Other ISP',organization,asn,asn_organization,connection_type,"
            "usage_type FROM enrichment_networks WHERE asn IS NOT NULL ORDER BY network_id "
            "LIMIT 1 RETURNING asn"
        ).fetchone()


def load_v9(database):
    """The live shape: the v9 release's database (its exact schema) with the current calculated
    layer (calculated v7, then v8's parts table and v9's lists)."""
    load_calculated(database, 6)
    schema = SCHEMA_V9.read_text()
    assert hashlib.sha256(schema.encode()).hexdigest() == V9_CHECKSUM
    calculated, calculated_checksum = store.schema()
    with connect(database) as conn:
        conn.execute((SOURCE / "migrate_v8_v9.sql").read_text())
        conn.execute(store.V6_SNAPSHOT)
        conn.execute((SOURCE / "calculated/migrate_v6_v7.sql").read_text())
        conn.execute((SOURCE / "calculated/migrate_v7_v8.sql").read_text())
        conn.execute((SOURCE / "calculated/migrate_v8_v9.sql").read_text())
        conn.execute(schema)
        conn.execute(calculated)
        conn.execute("UPDATE schema_versions SET version=9,checksum=%s", (V9_CHECKSUM,))
        conn.execute(
            "UPDATE calculation_schema SET version=%s,checksum=%s",
            (store.SCHEMA_VERSION, calculated_checksum),
        )
