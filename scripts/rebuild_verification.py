"""Logical invariants and read benchmarks for the controlled v3/v4/v5 rebuild."""

import hashlib
import json


def logical_snapshot(conn):
    version = conn.execute("SELECT max(version) FROM schema_versions").fetchone()[0]
    key = "run_key" if version >= 4 else "run_id"
    summary = "stored_run_summary(run_key)" if version >= 4 else "summary"
    queries = {
        "summaries": f"SELECT run_id::text,{summary},summary_sha256 FROM test_runs ORDER BY run_id",
        "addresses": "SELECT ip FROM ip_addresses ORDER BY ip",
        "membership": "SELECT provider,product,ip,first_seen,last_seen,observation_count,run_count "
        "FROM product_ip_details ORDER BY provider,product,ip",
        "observations": f"SELECT a.ip,r.run_id,o.bucket_start,o.count,o.first_seen,o.last_seen "
        f"FROM ip_observations o JOIN test_runs r USING({key}) "
        "JOIN ip_addresses a USING(ip_id) ORDER BY a.ip,r.run_id,o.bucket_start",
        "enrichment": "SELECT ip,dbip_release,matched,country_code,country,region_code,region,city,"
        "latitude,longitude,isp,organization,asn,asn_organization,connection_type,"
        "usage_type,extra_attributes FROM ip_enrichment_details ORDER BY ip,dbip_release",
        "run_enrichment": f"SELECT t.run_id,d.sha256,r.artifact_sha256,r.country_counts "
        f"FROM run_enrichments r JOIN test_runs t USING({key}) "
        "JOIN dbip_releases d USING(release_id) ORDER BY t.run_id,d.sha256",
        "releases": "SELECT sha256,metadata FROM dbip_releases ORDER BY sha256",
        # v5 rebuilds the same scope and metrics JSON from typed columns.
        "scope_metrics": "SELECT provider,product,target,scope_hash,scope,metrics "
        "FROM scope_details ORDER BY provider,product,target,scope_hash"
        if version >= 5
        else "SELECT p.provider,p.name,s.target,s.scope_hash,s.scope,s.metrics "
        "FROM scope_stats s JOIN products p USING(product_id) "
        "ORDER BY p.provider,p.name,s.target,s.scope_hash",
        "overlap": "SELECT provider_a,provider_b,shared_ips FROM provider_overlap "
        "ORDER BY provider_a,provider_b",
    }
    return {
        name: hashlib.sha256(
            json.dumps(conn.execute(query).fetchall(), default=str, sort_keys=True).encode()
        ).hexdigest()
        for name, query in queries.items()
    }


def sizes(conn):
    return {
        name: {"table_bytes": table, "index_bytes": index, "total_bytes": table + index}
        for name, table, index in conn.execute(
            "SELECT relname,pg_table_size(relid),pg_indexes_size(relid) "
            "FROM pg_stat_user_tables ORDER BY relname"
        )
    }


def benchmark(conn):
    version = conn.execute("SELECT max(version) FROM schema_versions").fetchone()[0]
    key = "run_key" if version >= 4 else "run_id"
    sample = conn.execute(
        f"SELECT {key},started_at,finished_at FROM test_runs ORDER BY started_at LIMIT 1"
    ).fetchone()
    if not sample:
        return {}
    queries = {
        "run_lookup": (f"SELECT count(*) FROM ip_observations WHERE {key}=%s", (sample[0],)),
        "time_range": (
            "SELECT sum(count) FROM ip_observations WHERE bucket_start >= "
            "date_trunc('hour',%s::timestamptz) AND bucket_start <= %s",
            sample[1:],
        ),
        "ip_history": (
            "SELECT o.* FROM ip_observations o WHERE ip_id=(SELECT min(ip_id) FROM ip_addresses)",
            (),
        ),
        "geo_filter": (
            "SELECT count(*) FROM ip_enrichment_details WHERE country_code='US' "
            "AND city='Los Angeles'",
            (),
        ),
        "isp_group": ("SELECT isp,count(*) FROM ip_enrichment_details GROUP BY isp", ()),
    }
    result = {}
    for name, (query, args) in queries.items():
        timings = []
        for _ in range(5):
            plan = conn.execute("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + query, args).fetchone()[
                0
            ][0]
            timings.append(plan["Execution Time"])
        result[name] = {"median_ms": sorted(timings)[2], "plan": plan}
    return result
