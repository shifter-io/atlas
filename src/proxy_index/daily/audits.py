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
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from psycopg.rows import dict_row

from .. import detect
from ..outages import _exists
from ..regions import REGION_IDS
from ..storage import connect, partition

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
