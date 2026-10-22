"""Read-only production audit with disposable TEMP tables for storage comparisons."""

import json
from pathlib import Path

from proxy_index.storage import connect


def size(conn, name):
    return dict(
        zip(
            ("heap_bytes", "index_bytes", "total_bytes"),
            conn.execute(
                "SELECT pg_table_size(%s::regclass),pg_indexes_size(%s::regclass),"
                "pg_total_relation_size(%s::regclass)",
                (name, name, name),
            ).fetchone(),
            strict=True,
        )
    )


with connect(".secrets/database.toml") as c:
    version = c.execute("SELECT max(version) FROM schema_versions").fetchone()[0]
    data = {"schema_version": version}
    key = "run_key" if version >= 4 else "run_id"
    source = f"ip_observations o JOIN test_runs r USING({key})"
    for name, run in [
        ("audit_uuid", "r.run_id"),
        ("audit_bigint", "dense_rank() OVER (ORDER BY r.run_id)"),
    ]:
        kind = "uuid" if name == "audit_uuid" else "bigint"
        c.execute(
            f"CREATE TEMP TABLE {name} (bucket_start timestamptz NOT NULL,"
            f"run_id {kind} NOT NULL,ip_id bigint NOT NULL,count bigint NOT NULL,"
            "first_seen timestamptz NOT NULL,last_seen timestamptz NOT NULL) ON COMMIT DROP"
        )
        c.execute(
            f"INSERT INTO {name} SELECT bucket_start,{run},ip_id,count,first_seen,last_seen "
            f"FROM {source} ORDER BY bucket_start,ip_id"
        )
        c.execute(f"ALTER TABLE {name} ADD PRIMARY KEY(bucket_start,run_id,ip_id)")
        c.execute(f"CREATE INDEX ON {name}(run_id)")
        c.execute(f"CREATE INDEX ON {name}(ip_id,bucket_start)")
        data[name] = size(c, name)
    c.execute("CREATE TEMP TABLE audit_compact (LIKE audit_bigint) ON COMMIT DROP")
    c.execute("INSERT INTO audit_compact SELECT * FROM audit_bigint")
    c.execute("ALTER TABLE audit_compact ADD PRIMARY KEY(run_id,bucket_start,ip_id)")
    c.execute("CREATE INDEX ON audit_compact(ip_id,bucket_start)")
    data["audit_bigint_two_indexes"] = size(c, "audit_compact")
    c.execute("CREATE TEMP TABLE audit_geo (LIKE ip_enrichment INCLUDING ALL) ON COMMIT DROP")
    c.execute("INSERT INTO audit_geo SELECT * FROM ip_enrichment")
    data["geo_current"] = size(c, "ip_enrichment")
    data["geo_fresh"] = size(c, "audit_geo")
    data["geo_rows"] = c.execute(
        "SELECT count(*),avg(pg_column_size(e)) FROM audit_geo e"
    ).fetchone()
    c.execute("CREATE TEMP TABLE audit_product (LIKE product_ips INCLUDING ALL) ON COMMIT DROP")
    c.execute("INSERT INTO audit_product SELECT * FROM product_ips")
    data["product_fresh"] = size(c, "audit_product")
    membership = "pool_ips" if version >= 4 else "scope_ips"
    c.execute(f"CREATE TEMP TABLE audit_scope (LIKE {membership} INCLUDING ALL) ON COMMIT DROP")
    c.execute(f"INSERT INTO audit_scope SELECT * FROM {membership}")
    data["scope_fresh"] = size(c, "audit_scope")
    data["scope_configs"] = c.execute(
        "SELECT scope-'config',scope->'config' FROM "
        + ("scope_details" if version >= 5 else "scope_stats")
    ).fetchall()
    data["field_cardinality"] = c.execute(
        "SELECT count(DISTINCT country_code),"
        "count(DISTINCT city),count(DISTINCT isp),count(DISTINCT asn),"
        "count(DISTINCT connection_type),count(DISTINCT usage_type) FROM ip_enrichment_details"
    ).fetchone()
    data["heap_layout"] = c.execute(
        "SELECT attname,attisdropped FROM pg_attribute "
        "WHERE attrelid='ip_enrichment'::regclass AND attnum>0 ORDER BY attnum"
    ).fetchall()
Path("artifacts/storage-comparison-latest.json").write_text(json.dumps(data, default=str, indent=2))
print(json.dumps(data, default=str, indent=2))
