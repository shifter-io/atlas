"""The DB-IP releases behind each observation (DBIP-DAILY-1, RELEASE-PER-RUN-1).

Each run keeps the release it was enriched with: its observations are read with that run's
newest entry in `run_enrichments`, never with the newest release overall. So a new release
(the daily update) changes only the runs enriched with it, and older runs keep their
locations. A release is its file's sha256 (`dbip_releases.sha256`), so a file replaced under
the same name with new content is a new release. Runs without enrichment (pulse) stay
unlabelled.

An IP that runs with different releases returned takes the labels of its lowest run_key in the
window: the rows come ordered by run_key and `aggregate.pool()` keeps an IP's first row.

The snapshot names every release its observations were labelled with (`dbip_releases`,
ascending ids) and the newest of them (`dbip_release`, null when it used none). A release no
observation of the window used (recorded by the update before any run used it, or used only by
runs outside the window) is in neither, so a build at an older cutoff names what it used.
"""

from __future__ import annotations

from typing import Any

RELEASE = "SELECT release_id,sha256,metadata FROM dbip_releases WHERE release_id=%s"
RUN_RELEASES = "SELECT run_key,max(release_id) AS release_id FROM run_enrichments GROUP BY run_key"
OBSERVATIONS = (
    f"WITH run_release AS ({RUN_RELEASES}) "
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


# The observations of some runs (the first parameter) only.
RUN_OBSERVATIONS = OBSERVATIONS.replace("WHERE ", "WHERE o.run_key=ANY(%s) AND ", 1)


def enriched_observations(conn, start, as_of) -> tuple[dict[str, Any], list]:
    """The snapshot's release fields (snapshot_fields()) and the window's observations, each
    with its run's release."""
    observations = conn.execute(OBSERVATIONS, (start, as_of)).fetchall()
    return used_releases(conn, {o["run_key"] for o in observations}), observations


def used_releases(conn, observed) -> dict[str, Any]:
    """The release fields of a window whose observations came from the runs observed."""
    per_run = {r["run_key"]: r["release_id"] for r in conn.execute(RUN_RELEASES)}
    used = sorted({per_run[k] for k in observed if k in per_run})
    newest = conn.execute(RELEASE, (used[-1],)).fetchone() if used else None
    return {"dbip_release": newest, "dbip_releases": used}


def snapshot_fields(release: dict[str, Any] | None) -> dict[str, Any]:
    """The payload's `dbip_release` and `dbip_releases` (none for a build given no release)."""
    return release or {"dbip_release": None, "dbip_releases": []}
