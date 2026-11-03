"""The DB-IP release behind each observation (DBIP-DAILY-1).

Each run keeps the release it was enriched with: its observations are read with that run's
newest entry in `run_enrichments`, never with the newest release overall. So a new release
(the daily update) changes only the runs enriched with it, and older runs keep their
locations. The snapshot names the newest release that some run uses; a release the update
recorded before any run used it changes nothing.
"""

from __future__ import annotations

from typing import Any

RELEASE = (
    "SELECT release_id,sha256,metadata FROM dbip_releases WHERE release_id IN "
    "(SELECT release_id FROM run_enrichments) ORDER BY release_id DESC LIMIT 1"
)
OBSERVATIONS = (
    "WITH run_release AS (SELECT run_key,max(release_id) AS release_id "
    "FROM run_enrichments GROUP BY run_key) "
    "SELECT o.*,host(a.ip) AS ip,e.matched,e.country_code,e.country,e.region_code,e.region,"
    "e.city,e.latitude,e.longitude,e.isp,e.organization,e.asn,e.asn_organization,"
    "e.connection_type,e.usage_type,m.first_seen AS product_first_seen "
    "FROM ip_observations o JOIN ip_addresses a USING(ip_id) "
    "JOIN test_runs r USING(run_key) JOIN scope_stats s USING(scope_id) "
    "JOIN product_ips m ON m.product_id=s.product_id AND m.ip_id=o.ip_id "
    "LEFT JOIN run_release rel ON rel.run_key=o.run_key "
    "LEFT JOIN ip_enrichment_fields e ON e.ip_id=o.ip_id AND e.release_id=rel.release_id "
    "WHERE o.bucket_start>=%s AND o.bucket_start<%s "
    "ORDER BY o.run_key,o.bucket_start,o.ip_id"
)


def enriched_observations(conn, start, as_of) -> tuple[dict[str, Any] | None, list]:
    """The snapshot's release and the window's observations, each with its run's release."""
    release = conn.execute(RELEASE).fetchone()
    return release, conn.execute(OBSERVATIONS, (start, as_of)).fetchall()
