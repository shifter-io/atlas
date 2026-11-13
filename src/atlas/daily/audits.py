"""The live tape's audits for an hour's daily tests (AUDIT-DAILY-1).

detect() writes the audits of finished batches; a daily test has no batch, so the daily ingest
calls audit() once an hour's tests are stored. It runs detect's own checks and thresholds
(configs/methodology.toml [events]) on the hour's runs:

- new_hosting_asn: the first datacenter or hosting ASN a network's exit addresses land in, once
  per network and ASN ever (detect._hosting), with the run's country, target and region;
- ip_overlap: once per network and tested hour, the network's addresses in the hour that other
  networks' daily tests of the same hour also exited from (detect.overlaps), as a census batch
  compares the providers of one census slot. data.hour names the hour.

Events are stamped with the time they are written, as detect stamps them, under detect's lock.
Running it again for the same hour and runs writes nothing.

`atlas daily audits` (backfill) runs audit() over the daily runs already stored in a time
range, hour by hour, to measure it or write the audits of hours stored before it existed
(AUDIT-DAILY-2). A run's hour is the UTC hour it started in, its campaign's hour, so the same
deduplication applies as for the ingest's call.
"""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from psycopg.rows import dict_row

from .. import detect, production_guard
from ..outages import _exists
from ..regions import REGION_IDS
from ..storage import connect, partition
from . import DAILY, MARKER

RUNS_SQL = (
    "SELECT t.run_key,t.run_id::text AS run_id,t.started_at AS run_started,"
    "t.finished_at AS run_finished,s.product_id,s.target AS target_id "
    "FROM test_runs t JOIN scope_stats s USING(scope_id) "
    "WHERE t.run_id=ANY(%s::uuid[]) ORDER BY t.run_key"
)
# The IPs of the hour's runs, as (product_id, provider, ip_id) rows for detect.overlaps().
RUN_IPS = (
    "SELECT DISTINCT s.product_id,p.provider,io.ip_id FROM test_runs t "
    "JOIN scope_stats s USING(scope_id) JOIN products p USING(product_id) "
    "JOIN ip_observations io ON io.run_key=t.run_key WHERE t.run_key=ANY(%s)"
)
# The daily runs that started in [since, until) (daily.MARKER), each with its tested hour (the
# UTC hour it started in), its country and its runner region: the region of its cell's
# daily_test_started event, of the same network, hour and country.
STORED_SQL = (
    "WITH r AS (SELECT t.run_id::text AS run_id,s.product_id,"
    "upper(ps.requested_country) AS country,"
    "date_trunc('hour',t.started_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AS hour "
    "FROM test_runs t JOIN scope_stats s USING(scope_id) JOIN pool_stats ps USING(pool_id) "
    "WHERE t.summary->>%s=%s AND t.started_at>=%s AND t.started_at<%s) "
    "SELECT r.run_id,r.hour,r.country,(SELECT e.data->>'region' FROM test_events e "
    "WHERE e.kind='daily_test_started' AND e.product_id=r.product_id AND e.at>=r.hour "
    "AND e.at<r.hour+interval '1 hour' AND e.data->'countries' ? r.country "
    "ORDER BY e.event_id LIMIT 1) AS region FROM r ORDER BY r.hour,r.run_id"
)
NO_BATCH = {"batch_id": None, "country_code": None, "target_id": None, "region_id": None}


def audit(conn, tested: dict, hour: datetime, now: datetime, *, config=None) -> int:
    """Write the audits of an hour's stored daily tests; returns how many.

    tested maps each stored run's id to its (country, runner region); hour is the tested hour.
    """
    cfg = config or detect.thresholds()
    cur = conn.cursor(row_factory=dict_row)
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (detect.LOCK,))
    runs = cur.execute(RUNS_SQL, (sorted(tested),)).fetchall()
    if not runs:
        return 0
    partition(conn, "test_events", now)
    emit = detect._Emitter(cur, now)
    for run in runs:
        country, region = tested[run["run_id"]]
        cell = {"country_code": country, "target_id": run["target_id"]}
        cell |= {"batch_id": None, "region_id": REGION_IDS[region], "region": region}
        detect._hosting(cur, cfg, cell, [run], emit)
    _overlap(cur, runs, hour, emit)
    return emit.count


def stored(credentials: Path, tested: dict, hour: datetime) -> int:
    """audit() now, in one transaction of its own over the credentials' database."""
    with connect(credentials) as conn:
        return audit(conn, tested, hour, datetime.now(UTC))


def _overlap(cur, runs, hour, emit):
    """One ip_overlap per network that shares addresses with another in the hour's tests."""
    if len({run["product_id"] for run in runs}) < 2:
        return
    # Any earlier event of the network for the hour, whenever written: a simulated hour may
    # lie after the moment it is stored.
    stamp = detect.timestamp(hour)
    for product, data in detect.overlaps(cur, RUN_IPS, ([r["run_key"] for r in runs],)):
        if _exists(
            cur,
            "SELECT 1 FROM test_events WHERE product_id=%s AND batch_id IS NULL "
            "AND kind='ip_overlap' AND data->>'hour'=%s",
            (product, stamp),
        ):
            continue
        runs_of = sum(1 for run in runs if run["product_id"] == product)
        emit("ip_overlap", NO_BATCH, product, (0, "info"), data | {"hour": stamp, "runs": runs_of})


def target_refusal(conn, owner_go: str | None, acknowledged: bool) -> str | None:
    """None unless the database is production's proxy_reviews without #817's three
    (production_guard.production_refusal); an accepted production run prints its banner."""
    database, role = conn.execute("SELECT current_database(),current_user").fetchone()
    if database != production_guard.PRODUCTION:
        return None
    now = datetime.now(UTC)
    found = production_guard.production_refusal(owner_go, acknowledged, now.date())
    if not found:
        banner = f"*** PRODUCTION DATABASE {database!r}: owner go {owner_go!r}, role {role!r}, "
        print(banner + f"{now.isoformat()} ***", file=sys.stderr)
    return found


def stored_hours(conn, since: datetime, until: datetime) -> list[tuple[datetime, dict, list]]:
    """(hour, tested, unmapped) of each hour, in order, with daily runs that started in
    [since, until): tested as the ingest passes it to audit(), and the run ids without a
    runner region (no daily_test_started event of their cell), which are left out."""
    hours: dict[datetime, tuple[dict, list]] = {}
    for run_id, hour, country, region in conn.execute(
        STORED_SQL, (MARKER, DAILY, since, until)
    ).fetchall():
        tested, unmapped = hours.setdefault(hour.astimezone(UTC), ({}, []))
        if region in REGION_IDS:
            tested[run_id] = (country, region)
        else:
            unmapped.append(run_id)
    return [(hour, *hours[hour]) for hour in sorted(hours)]


def backfill(conn, since: datetime, until: datetime, *, dry_run: bool = False):
    """Yield, hour by hour, what audit() writes for the daily runs stored in [since, until):
    the hour, its runs, the runs without a region, the events and the seconds it took.

    Each hour commits on its own, as the ingest's stored() call does. A dry run audits every
    hour in one transaction, so a later hour sees the earlier ones' events, then rolls back.
    """
    hours = stored_hours(conn, since, until)
    conn.commit()
    try:
        for hour, tested, unmapped in hours:
            began = time.monotonic()
            written = audit(conn, tested, hour, datetime.now(UTC)) if tested else 0
            if not dry_run:
                conn.commit()
            seconds = round(time.monotonic() - began, 3)
            report = {"hour": hour.isoformat(), "runs": len(tested), "unmapped": len(unmapped)}
            yield report | {"events": written, "seconds": seconds}
    finally:
        if dry_run:
            conn.rollback()
