"""storage.status: row counts and cached scope, pool and overlap totals."""

from __future__ import annotations

from psycopg import sql

from .storage_read import connect


def status(credentials_file):
    with connect(credentials_file) as conn:
        counts = {}
        for table in (
            "test_runs",
            "ip_addresses",
            "product_ips",
            "provider_overlap",
            "ip_observations",
            "run_buckets",
            "run_geo_buckets",
            "dbip_releases",
            "ip_enrichment",
            "run_enrichments",
            "scope_stats",
        ):
            counts[table] = conn.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
            ).fetchone()[0]
        return {
            "counts": counts,
            "cached_scopes": [
                {
                    "provider": row[0],
                    "product": row[1],
                    "target": row[2],
                    "scope_hash": row[3],
                    "metrics": row[4],
                }
                for row in conn.execute(
                    "SELECT provider,product,target,scope_hash,metrics FROM scope_details "
                    "ORDER BY scope_id"
                )
            ],
            "cached_pools": [
                {"provider": p, "product": product, "pool_hash": digest, "unique_ips": count}
                for p, product, digest, count in conn.execute(
                    "SELECT p.provider,p.name,s.pool_hash,s.unique_ips FROM pool_stats s "
                    "JOIN products p USING(product_id) ORDER BY p.provider,p.name,s.pool_hash"
                )
            ],
            "provider_overlaps": [
                {"provider_a": a, "provider_b": b, "shared_ips": count, "window": "lifetime"}
                for a, b, count in conn.execute(
                    "SELECT provider_a,provider_b,shared_ips FROM provider_overlap "
                    "ORDER BY provider_a,provider_b"
                )
            ],
        }
