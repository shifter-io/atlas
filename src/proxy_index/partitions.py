"""Range partitions of the raw tables, and how long raw rows stay in the database.

run_buckets, ip_observations and run_geo_buckets get one partition per UTC day
(`<table>_YYYY_MM_DD`), so the daily archive (proxy_index.archive) removes a day by dropping
it. A month that already has a monthly partition (`<table>_YYYY_MM`, created before daily
partitions) keeps using it until the archive has emptied and dropped it. test_events keeps
monthly partitions (the orchestrator's retention job drops them).
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

from psycopg import sql
from psycopg.rows import tuple_row

RAW_DAYS = 30  # raw rows are kept this long; the calculator reads no further back
DAILY = ("run_buckets", "ip_observations", "run_geo_buckets")
NAME = re.compile(r"(?P<y>\d{4})_(?P<m>\d{2})(?:_(?P<d>\d{2}))?")


def month_bounds(moment: datetime) -> tuple[datetime, datetime]:
    start = moment.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start, (start + timedelta(days=32)).replace(day=1)


def partition(conn, table, moment):
    """Create the partition of a range-partitioned table that holds this UTC moment, unless it
    exists; `conn` must be in a transaction (creation()).

    Daily for the DAILY tables unless that month has a monthly partition; monthly for the
    others (test_events, by `at`). A partition that exists costs one lookup and no lock.
    """
    name, start, end = monthly(table, moment)
    if table in DAILY and not _exists(conn, name):
        start = moment.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        end, name = start + timedelta(days=1), f"{table}_{start:%Y_%m_%d}"
    if not _exists(conn, name):
        for query in creation(table, name, start, end):
            conn.execute(query)


def monthly(table, moment):
    """The monthly partition of a table that holds this UTC moment: (name, start, end)."""
    start, end = month_bounds(moment)
    return f"{table}_{start:%Y_%m}", start, end


def creation(table, name, start, end):
    """The statements that create one partition, to run in one transaction.

    CREATE TABLE IF NOT EXISTS ... PARTITION OF looks for the name before it locks the parent,
    so sessions creating the same missing partition at once all find it missing, and each but
    the first fails with DuplicateTable once the first commits. Taking the parent's ACCESS
    EXCLUSIVE lock first (the lock the CREATE takes anyway, held until the transaction ends;
    ONLY: not its partitions) makes them create one at a time: the later ones find it and skip.
    """
    return (
        sql.SQL("LOCK TABLE ONLY {} IN ACCESS EXCLUSIVE MODE").format(sql.Identifier(table)),
        sql.SQL(
            "CREATE TABLE IF NOT EXISTS {} PARTITION OF {} FOR VALUES FROM ({}) TO ({})"
        ).format(sql.Identifier(name), sql.Identifier(table), sql.Literal(start), sql.Literal(end)),
    )


def _exists(conn, name):
    found = conn.cursor(row_factory=tuple_row).execute("SELECT to_regclass(%s)", (name,))
    return found.fetchone()[0] is not None


def children(conn, table) -> list[tuple[str, date, date]]:
    """The named partitions of a table: (name, first day, day after the last), oldest first.

    Partitions not named `<table>_YYYY_MM[_DD]` are left out.
    """
    rows = conn.cursor(row_factory=tuple_row).execute(
        "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid=i.inhrelid "
        "WHERE i.inhparent=%s::regclass",
        (table,),
    )
    found = []
    for (name,) in rows.fetchall():
        match = NAME.fullmatch(name.removeprefix(f"{table}_"))
        if not name.startswith(f"{table}_") or not match:
            continue
        first = date(int(match["y"]), int(match["m"]), int(match["d"] or 1))
        if match["d"]:
            after = first + timedelta(days=1)
        else:
            after = (first + timedelta(days=32)).replace(day=1)
        found.append((name, first, after))
    return sorted(found, key=lambda item: item[1])
