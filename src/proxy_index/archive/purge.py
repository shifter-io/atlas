"""Delete an archived day from the database, only when it still matches its manifest.

One transaction under the ingest lock: the day's rows are selected again, links from rows that
stay (tape events, outages, other days' retries) to the day's batches are set NULL, the day's
facts are deleted (a daily partition holding only the day's exported rows is dropped whole,
other rows by run within the day's run times), and every deleted count must equal the
manifest's row count, else everything rolls back. Afterwards empty daily partitions up to the
day and empty monthly partitions of ended months are dropped.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from psycopg import sql

from ..partitions import DAILY, RAW_DAYS, children
from ..storage import connect
from . import ArchiveError
from .tables import BY_NAME, day_range, present, select_day

INGEST_LOCK = 817452901  # storage.ingest's advisory lock: no import runs during a purge
_UNLINK = (
    "UPDATE test_events SET batch_id=NULL WHERE batch_id IN (SELECT batch_id FROM day_batches)",
    "UPDATE outages SET batch_id=NULL WHERE batch_id IN (SELECT batch_id FROM day_batches)",
    "UPDATE test_batches SET retry_of=NULL WHERE retry_of IN (SELECT batch_id FROM day_batches) "
    "AND batch_id NOT IN (SELECT batch_id FROM day_batches)",
)
_BY_RUN = ("calculation_roles", "estimated_hours", "run_enrichments")
# Runs whose buckets can lie outside the day's partition (they cross a midnight).
_SPILL = "(d.started_at<%(lo)s+interval '1 hour' OR d.finished_at>=%(hi)s-interval '1 hour')"


def _delete(conn, statement: str, params: dict | None = None) -> int:
    return conn.execute(statement, params).rowcount


def _drop_own_partition(conn, table: str, day: date, params: dict) -> int | None:
    """Drop the day's daily partition when it holds only exported rows; its row count."""
    own = f"{table}_{day:%Y_%m_%d}"
    if own not in [name for name, _, _ in children(conn, table)]:
        return None
    name = sql.Identifier(own)
    total, foreign = conn.execute(
        sql.SQL(
            "SELECT count(*),count(*) FILTER (WHERE d.run_key IS NULL OR p.bucket_start<%(b_lo)s "
            "OR p.bucket_start>=%(b_hi)s) FROM {} p LEFT JOIN day_runs d USING(run_key)"
        ).format(name),
        params,
    ).fetchone()
    if foreign:
        return None
    conn.execute(sql.SQL("DROP TABLE {}").format(name))
    return total


def _purge_partitioned(conn, table: str, day: date, params: dict) -> int:
    dropped = _drop_own_partition(conn, table, day, params)
    spill = f" AND {_SPILL}" if dropped is not None else ""
    deleted = _delete(
        conn,
        f"DELETE FROM {table} t USING day_runs d WHERE t.run_key=d.run_key "
        f"AND t.bucket_start>=%(b_lo)s AND t.bucket_start<%(b_hi)s{spill}",
        params,
    )
    return deleted + (dropped or 0)


def _drop_empty(conn, day: date) -> list[str]:
    """Empty daily partitions up to the day, and empty monthly ones of months ended by then."""
    dropped = []
    for table in DAILY:
        for name, first, after in children(conn, table):
            daily = after - first == timedelta(days=1)
            if first > day or (not daily and after > day + timedelta(days=1)):
                continue
            ident = sql.Identifier(name)
            if conn.execute(sql.SQL("SELECT 1 FROM {} LIMIT 1").format(ident)).fetchone():
                continue
            conn.execute(sql.SQL("DROP TABLE {}").format(ident))
            dropped.append(name)
    return dropped


def _delete_facts(conn, day: date, params: dict) -> dict[str, int]:
    for statement in _UNLINK:
        conn.execute(statement)
    deleted = {}
    for name in _BY_RUN:
        if present(conn, BY_NAME[name]):
            deleted[name] = _delete(
                conn, f"DELETE FROM {name} t USING day_runs d WHERE t.run_key=d.run_key"
            )
    for table in DAILY:
        deleted[table] = _purge_partitioned(conn, table, day, params)
    deleted["batch_runs"] = _delete(
        conn, "DELETE FROM batch_runs WHERE batch_id IN (SELECT batch_id FROM day_batches)"
    )
    deleted["test_runs"] = _delete(
        conn, "DELETE FROM test_runs WHERE run_key IN (SELECT run_key FROM day_runs)"
    )
    deleted["test_batches"] = _delete(
        conn, "DELETE FROM test_batches WHERE batch_id IN (SELECT batch_id FROM day_batches)"
    )
    return deleted


def pending(conn, day: date, now: datetime) -> str | None:
    """Why the selected day cannot be deleted yet, or None when it is due.

    Due: none of its batches or batch runs is still open, and its last run end, batch end
    and estimated hour end are at least RAW_DAYS old, so no calculator window reads it.
    """
    row = conn.execute(
        "SELECT (SELECT count(*) FROM test_batches JOIN day_batches USING(batch_id) "
        "WHERE status IN ('scheduled','running'))+(SELECT count(*) FROM batch_runs "
        "JOIN day_batches USING(batch_id) WHERE status IN ('queued','running','uploading')),"
        "greatest((SELECT max(finished_at) FROM day_runs),"
        "(SELECT max(coalesce(r.finished_at,r.started_at)) FROM batch_runs r "
        "JOIN day_batches USING(batch_id)),(SELECT max(finished_at) FROM test_batches "
        "JOIN day_batches USING(batch_id)),(SELECT max((e.day+(e.hour+1)*interval '1 hour') "
        "AT TIME ZONE 'UTC') FROM estimated_hours e JOIN day_runs USING(run_key)))"
    ).fetchone()
    if row[0]:
        return f"{row[0]} batches or batch runs of the day are still open"
    last = max(day_range(day)[1], row[1] or day_range(day)[1])
    if last + timedelta(days=RAW_DAYS) > now:
        return f"rows of the day end at {last.isoformat()}, less than {RAW_DAYS} days ago"
    return None


def purge_day(
    database, day: date, manifest: dict, now: datetime, *, lock_timeout: str = "60s"
) -> dict:
    """Delete the day's facts; returns the deleted rows per table and the dropped partitions.

    Raises ArchiveError (nothing deleted) when the manifest is of another day, the day is not
    due, or a table's rows differ in number from the manifest's.
    """
    if manifest.get("day") != day.isoformat():
        raise ArchiveError(f"the manifest is not of {day}")
    expected = {t["name"]: t["rows"] for t in manifest["tables"] if t["role"] == "fact"}
    with connect(database) as conn:
        conn.execute(sql.SQL("SET LOCAL lock_timeout={}").format(sql.Literal(lock_timeout)))
        conn.execute("SET LOCAL statement_timeout=0")
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (INGEST_LOCK,))
        params = select_day(conn, day)
        reason = pending(conn, day, now)
        if reason:
            raise ArchiveError(f"{day} is not due: {reason}")
        deleted = _delete_facts(conn, day, params)
        differ = {
            name: {"archived": expected.get(name, 0), "deleted": deleted.get(name, 0)}
            for name in sorted(set(expected) | set(deleted))
            if deleted.get(name, 0) != expected.get(name, 0)
        }
        if differ:  # raising rolls the transaction back
            raise ArchiveError(f"{day} changed since it was archived: {differ}")
        partitions = _drop_empty(conn, day)
    return {"day": day.isoformat(), "deleted": deleted, "dropped_partitions": partitions}
