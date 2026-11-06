"""The batch page's data: one read of a batch, its runs, their latency, timeline and geography.

`batch_data(conn, batch_id)` returns the batch row with its `runs` (each with p50/p90/p99, new
IPs, the 5-minute timeline and the top cities and ASNs), the census `min_sample` setting and
the batch's tape `events`; report_html.py renders it. `_percentiles` and `_keyed` are the query
helpers the report pages share. Nothing here renders HTML.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import psycopg

from .orchestrator.control import read_setting
from .report_parts import TOP

# The geography rows of a batch's runs, per product, location and network.
_GEO = (
    "FROM batch_runs br JOIN run_geo_buckets g ON g.run_key=br.run_key "
    "AND g.bucket_start>=%(since)s "
    "JOIN enrichment_location_fields l ON l.location_id=g.location_id "
    "JOIN enrichment_network_fields n ON n.network_id=g.network_id WHERE br.batch_id=%(batch)s"
)
_CITY_KEYS = ["product_id", "region", "city"]
_ASN_KEYS = ["product_id", "asn"]


def _percentiles(source: str, keys: list[str]) -> str:
    """p50/p90/p99 per key over a source of (keys..., steps, counts) histogram rows, exactly
    as stats.Histogram ranks them."""
    k = ",".join(keys)
    return (
        f"WITH s AS ({source}), h AS (SELECT {k},x.ms,sum(x.n) AS n FROM s CROSS JOIN LATERAL "
        f"histogram(s.steps,s.counts::bigint[]) x GROUP BY {k},x.ms), c AS (SELECT {k},ms,"
        f"sum(n) OVER (PARTITION BY {k} ORDER BY ms) AS seen,sum(n) OVER (PARTITION BY {k}) "
        f"AS total FROM h) SELECT {k},min(ms) FILTER (WHERE seen>=ceil(total*0.5)) AS p50,"
        "min(ms) FILTER (WHERE seen>=ceil(total*0.9)) AS p90,"
        f"min(ms) FILTER (WHERE seen>=ceil(total*0.99)) AS p99 FROM c GROUP BY {k}"
    )


def _keyed(rows: list[dict], keys: list[str]) -> dict[tuple, dict]:
    return {tuple(row[k] for k in keys): row for row in rows}


def batch_data(conn: psycopg.Connection, batch_id: int) -> dict[str, Any] | None:
    batch = conn.execute(
        "SELECT b.batch_id,b.kind,b.country_code,c.name AS country,r.slug AS region,b.target_id,"
        "t.label AS target_label,t.url AS target_url,b.scheduled_for,b.start_at,b.started_at,"
        "b.finished_at,b.status FROM test_batches b JOIN runner_regions r "
        "ON r.region_id=b.region_id JOIN countries c ON c.country_code=b.country_code "
        "JOIN targets t ON t.target_id=b.target_id WHERE b.batch_id=%s",
        (batch_id,),
    ).fetchone()
    if batch is None:
        return None
    params = {"batch": batch_id, "since": batch["scheduled_for"] - timedelta(days=1)}
    runs = _runs(conn, params)
    _add_run_stats(conn, params, runs)
    _add_geography(conn, params, runs)
    batch["runs"] = runs
    batch["min_sample"] = int(read_setting(conn, "census.min_sample"))
    batch["events"] = conn.execute(
        "SELECT e.at,e.kind,p.provider,p.name AS product,e.signal,e.severity,e.data "
        "FROM test_events e LEFT JOIN products p USING(product_id) "
        "WHERE e.batch_id=%(batch)s AND e.at>=%(since)s ORDER BY e.at,e.event_id",
        params,
    ).fetchall()
    return batch


def _runs(conn: psycopg.Connection, params: dict[str, Any]) -> list[dict]:
    return conn.execute(
        "SELECT br.product_id,p.provider,p.name AS product,br.node_id,n.host,br.status,"
        "br.stop_reason,br.error,br.started_at,br.finished_at,br.requests,br.successes,"
        "br.unique_ips,br.challenges,br.blocks,br.timeouts,br.proxy_errors,br.bytes_up,"
        "br.bytes_down,br.run_key,br.last_progress_at,n.version AS node_version,"
        "tr.summary->'config_overrides'->>'census_rule_version' AS census_rule_version "
        "FROM batch_runs br "
        "JOIN products p USING(product_id) "
        "LEFT JOIN nodes n ON n.node_id=br.node_id "
        "LEFT JOIN test_runs tr ON tr.run_key=br.run_key WHERE br.batch_id=%(batch)s "
        "ORDER BY p.provider,p.name",
        params,
    ).fetchall()


def _add_run_stats(conn: psycopg.Connection, params: dict[str, Any], runs: list[dict]) -> None:
    """Each run's latency percentiles, new IPs and 5-minute timeline."""
    latency = _keyed(
        conn.execute(
            _percentiles(
                "SELECT br.product_id,b.histogram_steps AS steps,b.histogram_counts AS counts "
                "FROM batch_runs br JOIN run_buckets b ON b.run_key=br.run_key "
                "AND b.bucket_start>=%(since)s WHERE br.batch_id=%(batch)s",
                ["product_id"],
            ),
            params,
        ).fetchall(),
        ["product_id"],
    )
    new_ips = _keyed(
        conn.execute(
            "SELECT br.product_id,count(*)::bigint AS new_ips FROM batch_runs br "
            "JOIN test_runs tr ON tr.run_key=br.run_key "
            "CROSS JOIN LATERAL (SELECT DISTINCT o.ip_id FROM ip_observations o "
            " WHERE o.run_key=br.run_key AND o.bucket_start>=%(since)s) o "
            "JOIN product_ips pi ON pi.product_id=br.product_id AND pi.ip_id=o.ip_id "
            "WHERE br.batch_id=%(batch)s AND pi.first_seen>=tr.started_at GROUP BY 1",
            params,
        ).fetchall(),
        ["product_id"],
    )
    timelines: dict[int, list[dict]] = {}
    for row in conn.execute(
        "SELECT br.product_id,b.bucket_start,sum(b.completed)::bigint AS requests,"
        "sum(b.successes)::bigint AS successes FROM batch_runs br JOIN run_buckets b "
        "ON b.run_key=br.run_key AND b.bucket_start>=%(since)s WHERE br.batch_id=%(batch)s "
        "GROUP BY 1,2 ORDER BY 1,2",
        params,
    ):
        timelines.setdefault(row["product_id"], []).append(row)
    for run in runs:
        pid = run["product_id"]
        run.update(latency.get((pid,), {"p50": None, "p90": None, "p99": None}))
        run["new_ips"] = (
            new_ips[(pid,)]["new_ips"] if (pid,) in new_ips else (0 if run["run_key"] else None)
        )
        run["timeline"] = timelines.get(pid, [])


def _add_geography(conn: psycopg.Connection, params: dict[str, Any], runs: list[dict]) -> None:
    """Each run's top cities and ASNs, with their p50 latency and, per ASN, distinct IPs."""
    cities, city_latency = _cities(conn, params)
    asns, asn_latency, asn_ips = _asns(conn, params)
    for run in runs:
        pid = run["product_id"]
        run["cities"] = [
            c | {"p50": city_latency.get(tuple(c[k] for k in _CITY_KEYS), {}).get("p50")}
            for c in cities
            if c["product_id"] == pid
        ][:TOP]
        run["asns"] = [
            a
            | {
                "p50": asn_latency.get((pid, a["asn"]), {}).get("p50"),
                "ips": asn_ips.get((pid, a["asn"]), {}).get("ips"),
            }
            for a in asns
            if a["product_id"] == pid
        ][:TOP]


def _cities(
    conn: psycopg.Connection, params: dict[str, Any]
) -> tuple[list[dict], dict[tuple, dict]]:
    cities = conn.execute(
        "SELECT br.product_id,l.region,l.city,sum(g.completed)::bigint AS requests,"
        f"sum(g.successes)::bigint AS successes {_GEO} GROUP BY 1,2,3 "
        "ORDER BY 4 DESC,2,3",
        params,
    ).fetchall()
    city_latency = _keyed(
        conn.execute(
            _percentiles(
                "SELECT br.product_id,l.region,l.city,g.histogram_steps AS steps,"
                f"g.histogram_counts AS counts {_GEO}",
                _CITY_KEYS,
            ),
            params,
        ).fetchall(),
        _CITY_KEYS,
    )
    return cities, city_latency


def _asns(
    conn: psycopg.Connection, params: dict[str, Any]
) -> tuple[list[dict], dict[tuple, dict], dict[tuple, dict]]:
    asns = conn.execute(
        "SELECT br.product_id,n.asn,max(coalesce(n.asn_organization,n.organization)) AS org,"
        "string_agg(DISTINCT n.usage_type,', ') AS usage_type,"
        "sum(g.completed)::bigint AS requests,sum(g.successes)::bigint AS successes "
        f"{_GEO} GROUP BY 1,2 ORDER BY 5 DESC,2",
        params,
    ).fetchall()
    asn_latency = _keyed(
        conn.execute(
            _percentiles(
                "SELECT br.product_id,n.asn,g.histogram_steps AS steps,"
                f"g.histogram_counts AS counts {_GEO}",
                _ASN_KEYS,
            ),
            params,
        ).fetchall(),
        _ASN_KEYS,
    )
    asn_ips = _keyed(
        conn.execute(
            "SELECT br.product_id,n.asn,count(DISTINCT o.ip_id)::bigint AS ips "
            "FROM batch_runs br JOIN ip_observations o ON o.run_key=br.run_key "
            "AND o.bucket_start>=%(since)s "
            "JOIN LATERAL (SELECT max(release_id) AS release_id FROM run_enrichments re "
            " WHERE re.run_key=br.run_key) re ON true "
            "JOIN ip_enrichment e ON e.ip_id=o.ip_id AND e.release_id=re.release_id "
            "JOIN enrichment_network_fields n ON n.network_id=e.network_id "
            "WHERE br.batch_id=%(batch)s GROUP BY 1,2",
            params,
        ).fetchall(),
        _ASN_KEYS,
    )
    return asns, asn_latency, asn_ips
