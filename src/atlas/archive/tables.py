"""The tables of a day file, and which of their rows belong to a UTC day.

A batch belongs to the UTC day of its `scheduled_for`; a run to its batch's day, else (daily
tests have no batch) to the UTC day of its `started_at`. Every row tied to a run or batch goes
with it. `select_day` fills the session's temporary tables `day_batches` and `day_runs` (and,
for an export, the referenced dimension ids) that the queries below read; the bucket tables
are read within the day's run times plus a margin (`b_lo`, `b_hi`), so only the partitions
around the day are scanned.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

MARGIN = timedelta(hours=1)  # bucket rows of a run lie within its start and finish (5-min floor)
# Session settings that make an export's text, and so its checksums, independent of the server.
SETTINGS = (
    "SET LOCAL TimeZone='UTC'",
    "SET LOCAL DateStyle='ISO, YMD'",
    "SET LOCAL IntervalStyle='postgres'",
    "SET LOCAL extra_float_digits=1",
    "SET LOCAL statement_timeout=0",
)


@dataclass(frozen=True)
class Table:
    """One CSV of a day file.

    role: "fact" rows are the day's own and are deleted once archived; "dimension" rows are
    kept in the database and copied so the file loads on its own; "copy" rows are kept and
    copied. key/natural drive the restore's id check: a row with the same key must have the
    same natural values and, when `unique`, the same natural values must have the same key.
    """

    name: str
    query: str
    role: str = "dimension"
    key: tuple[str, ...] = ()
    natural: tuple[str, ...] = ()
    unique: bool = False
    nullable: bool = False  # natural values may be NULL (compared with IS NOT DISTINCT FROM)


def _of_runs(table: str, order: str, bounded: bool = False) -> str:
    where = " WHERE t.bucket_start>={b_lo} AND t.bucket_start<{b_hi}" if bounded else ""
    return f"SELECT t.* FROM {table} t JOIN day_runs d USING(run_key){where} ORDER BY {order}"


_PLACE = ("country_code", "country", "region_code", "region", "city")
_NETWORK = ("asn_name_id", "isp", "organization", "connection_type", "usage_type")
TABLES = (
    Table("providers", "SELECT * FROM providers ORDER BY id", key=("id",)),
    Table(
        "products",
        "SELECT * FROM products ORDER BY product_id",
        key=("product_id",),
        natural=("provider", "name"),
        unique=True,
    ),
    Table(
        "runner_regions",
        "SELECT * FROM runner_regions ORDER BY region_id",
        key=("region_id",),
        natural=("slug",),
        unique=True,
    ),
    Table("countries", "SELECT * FROM countries ORDER BY country_code", key=("country_code",)),
    Table("targets", "SELECT * FROM targets ORDER BY target_id", key=("target_id",)),
    # Nodes are copied without their token hash.
    Table(
        "nodes",
        "SELECT node_id,region_id,provider,product,'archived-'||node_id AS token_hash "
        "FROM nodes WHERE node_id IN (SELECT r.node_id FROM batch_runs r "
        "JOIN day_batches USING(batch_id)) ORDER BY node_id",
        key=("node_id",),
        natural=("region_id", "provider", "product"),
        unique=True,
    ),
    Table(
        "place_names",
        "SELECT * FROM place_names WHERE place_id IN (SELECT place_id FROM "
        "enrichment_locations JOIN day_locations USING(location_id)) ORDER BY place_id",
        key=("place_id",),
        natural=_PLACE,
        unique=True,
        nullable=True,
    ),
    Table(
        "asn_names",
        "SELECT * FROM asn_names WHERE asn_name_id IN (SELECT asn_name_id FROM "
        "enrichment_networks JOIN day_networks USING(network_id)) ORDER BY asn_name_id",
        key=("asn_name_id",),
        natural=("asn", "name"),
        unique=True,
        nullable=True,
    ),
    Table(
        "enrichment_locations",
        "SELECT t.* FROM enrichment_locations t JOIN day_locations USING(location_id) "
        "ORDER BY location_id",
        key=("location_id",),
        natural=("place_id", "latitude", "longitude"),
        unique=True,
        nullable=True,
    ),
    Table(
        "enrichment_networks",
        "SELECT t.* FROM enrichment_networks t JOIN day_networks USING(network_id) "
        "ORDER BY network_id",
        key=("network_id",),
        natural=_NETWORK,
        unique=True,
        nullable=True,
    ),
    Table(
        "dbip_releases",
        "SELECT t.* FROM dbip_releases t JOIN day_releases USING(release_id) ORDER BY release_id",
        key=("release_id",),
        natural=("sha256",),
        unique=True,
    ),
    Table(
        "ip_addresses",
        "SELECT t.* FROM ip_addresses t JOIN day_ips USING(ip_id) ORDER BY ip_id",
        key=("ip_id",),
        natural=("ip",),
        unique=True,
    ),
    Table(
        "ip_enrichment",
        "SELECT t.* FROM ip_enrichment t JOIN day_enrichment USING(ip_id,release_id) "
        "ORDER BY ip_id,release_id",
        key=("ip_id", "release_id"),
        natural=("location_id", "network_id"),
    ),
    Table(
        "pool_stats",
        "SELECT * FROM pool_stats WHERE pool_id IN (SELECT s.pool_id FROM scope_stats s "
        "JOIN day_runs USING(scope_id)) ORDER BY pool_id",
        key=("pool_id",),
        natural=("product_id", "pool_hash"),
        unique=True,
    ),
    Table(
        "scope_stats",
        "SELECT * FROM scope_stats WHERE scope_id IN (SELECT scope_id FROM day_runs) "
        "ORDER BY scope_id",
        key=("scope_id",),
        natural=("product_id", "target", "scope_hash"),
        unique=True,
    ),
    Table(
        "test_batches",
        "SELECT t.* FROM test_batches t JOIN day_batches USING(batch_id) ORDER BY batch_id",
        "fact",
        ("batch_id",),
        ("kind", "country_code", "target_id", "scheduled_for"),
        unique=True,
    ),
    Table("test_runs", _of_runs("test_runs", "run_key"), "fact", ("run_key",), ("run_id",), True),
    Table(
        "batch_runs",
        "SELECT t.* FROM batch_runs t JOIN day_batches USING(batch_id) "
        "ORDER BY batch_id,product_id",
        "fact",
    ),
    Table("run_buckets", _of_runs("run_buckets", "run_key,bucket_start", True), "fact"),
    Table(
        "run_geo_buckets",
        _of_runs("run_geo_buckets", "run_key,bucket_start,location_id,network_id", True),
        "fact",
    ),
    Table(
        "ip_observations", _of_runs("ip_observations", "run_key,bucket_start,ip_id", True), "fact"
    ),
    Table("run_enrichments", _of_runs("run_enrichments", "run_key,release_id"), "fact"),
    Table(
        "estimated_hours",
        _of_runs("estimated_hours", "product_id,country_code,day,hour"),
        "fact",
    ),
    Table("calculation_roles", _of_runs("calculation_roles", "run_key"), "fact"),
    Table(
        "test_events",
        "SELECT * FROM test_events WHERE at>={lo} AND at<{hi} ORDER BY event_id",
        "copy",
    ),
)
BY_NAME = {table.name: table for table in TABLES}
# The partition column of the range-partitioned tables.
PARTITIONED = {
    "run_buckets": "bucket_start",
    "ip_observations": "bucket_start",
    "run_geo_buckets": "bucket_start",
    "test_events": "at",
}
# Links that may point outside a file: (column, parent table); a restore sets them NULL then.
OPTIONAL = {
    "test_batches": ("retry_of", "test_batches"),
    "test_events": ("batch_id", "test_batches"),
}

_DAY = (
    "CREATE TEMP TABLE day_batches ON COMMIT DROP AS SELECT batch_id FROM test_batches "
    "WHERE scheduled_for>=%(lo)s AND scheduled_for<%(hi)s",
    "CREATE TEMP TABLE day_runs ON COMMIT DROP AS SELECT r.run_key,r.scope_id,r.started_at,"
    "r.finished_at FROM test_runs r LEFT JOIN test_batches b USING(batch_id) "
    "WHERE coalesce(b.scheduled_for,r.started_at)>=%(lo)s "
    "AND coalesce(b.scheduled_for,r.started_at)<%(hi)s",
    "ALTER TABLE day_batches ADD PRIMARY KEY(batch_id)",
    "ALTER TABLE day_runs ADD PRIMARY KEY(run_key)",
    "ANALYZE day_batches",
    "ANALYZE day_runs",
)
# The dimension rows an export copies: ids referenced by the day's facts.
_DIMENSIONS = (
    "CREATE TEMP TABLE day_ips ON COMMIT DROP AS SELECT DISTINCT o.ip_id FROM ip_observations o "
    "JOIN day_runs USING(run_key) WHERE o.bucket_start>=%(b_lo)s AND o.bucket_start<%(b_hi)s",
    "CREATE TEMP TABLE day_releases ON COMMIT DROP AS SELECT DISTINCT release_id "
    "FROM run_enrichments JOIN day_runs USING(run_key)",
    "CREATE TEMP TABLE day_enrichment ON COMMIT DROP AS SELECT e.ip_id,e.release_id,"
    "e.location_id,e.network_id FROM ip_enrichment e JOIN day_ips USING(ip_id) "
    "JOIN day_releases USING(release_id)",
    "CREATE TEMP TABLE day_locations ON COMMIT DROP AS SELECT g.location_id "
    "FROM run_geo_buckets g JOIN day_runs USING(run_key) "
    "WHERE g.bucket_start>=%(b_lo)s AND g.bucket_start<%(b_hi)s "
    "UNION SELECT location_id FROM day_enrichment",
    "CREATE TEMP TABLE day_networks ON COMMIT DROP AS SELECT g.network_id "
    "FROM run_geo_buckets g JOIN day_runs USING(run_key) "
    "WHERE g.bucket_start>=%(b_lo)s AND g.bucket_start<%(b_hi)s "
    "UNION SELECT network_id FROM day_enrichment",
)


def day_range(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return start, start + timedelta(days=1)


def select_day(conn, day: date, *, dimensions: bool = False) -> dict[str, datetime]:
    """Fill the day's temporary tables in this transaction; returns the query bounds."""
    lo, hi = day_range(day)
    params = {"lo": lo, "hi": hi}
    for statement in _DAY:
        conn.execute(statement, params)
    first, last = conn.execute("SELECT min(started_at),max(finished_at) FROM day_runs").fetchone()
    params["b_lo"] = first - MARGIN if first else lo
    params["b_hi"] = last + MARGIN if last else lo
    if dimensions:
        for statement in _DIMENSIONS:
            conn.execute(statement, params)
    return params


def present(conn, table: Table) -> bool:
    return conn.execute("SELECT to_regclass(%s)", (table.name,)).fetchone()[0] is not None
