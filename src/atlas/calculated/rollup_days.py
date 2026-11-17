"""The per-day geo rollups (CALC-SCALE-2, calculated v19): each whole UTC day's hour rollups
(calculated_geo_hours, rollups.py) summed per key, so a build reads 30 days of geo rows from about
30 days' keys instead of 720 hours' rows.

Every geo counter and light-load histogram count is a sum, so a day's row is exactly the sum of
its 24 hour rows: a geo row read from the days of its range and the hours outside them equals the
one read from the hours alone. A build reads a day from here when the day lies wholly inside one
of its geo ranges (the 720, 168 and 24 hours before the cutoff: its rows' time, the day's start,
classes them, scheduled.GEO_KEY) and the day is marked; every other hour of the ranges, and the
hours a cut falls inside, are read as before.

rollups.fill() writes the days ahead of the build, committed (write()), once every hour of a day
is counted; the build only reads them, so its worker processes, which read the build's snapshot
and not its own writes (parallel_measure.py), see the same days. Counting an hour again (a late
run, a verdict, rollups._count()) deletes its day and the day's mark (forget()), so a day is read
only while its hours are the ones it summed; a changed country rule deletes every day with the
hours (forget_all()). Days over a day older than the window read are collected (collect()).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise

from psycopg.rows import tuple_row

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
KEYS = "scope_id,product_id,location_id,network_id,fallback,target,run_key"
# A day's row per key: its hours' counters summed, and their light-load bins summed per ms.
WRITE = (
    f"INSERT INTO calculated_geo_days SELECT %(day)s,{KEYS},sum(completed),sum(successes),"
    "sum(challenges),sum(blocks),"
    "coalesce(array_agg(ms ORDER BY ms) FILTER (WHERE ms IS NOT NULL),'{}'),"
    "coalesce(array_agg(n ORDER BY ms) FILTER (WHERE ms IS NOT NULL),'{}') FROM ("
    f"SELECT {KEYS},completed,successes,challenges,blocks,NULL::bigint AS ms,NULL::bigint AS n "
    "FROM calculated_geo_hours WHERE hour>=%(day)s AND hour<%(next)s UNION ALL "
    f"SELECT {KEYS},NULL,NULL,NULL,NULL,x.ms,sum(x.n)::bigint FROM calculated_geo_hours h "
    "CROSS JOIN LATERAL unnest(h.light_ms,h.light_n) x(ms,n) "
    f"WHERE h.hour>=%(day)s AND h.hour<%(next)s GROUP BY {KEYS},x.ms) u GROUP BY {KEYS}"
)
MARK = "INSERT INTO calculated_geo_day_marks VALUES (%s) ON CONFLICT DO NOTHING"
MARKED = "SELECT day FROM calculated_geo_day_marks WHERE day=ANY(%s)"
TABLES = ("calculated_geo_days", "calculated_geo_day_marks")
# The days holding an hour of [lo, hi).
FORGET = "DELETE FROM {} WHERE day>%(lo)s::timestamptz-interval '1 day' AND day<%(hi)s"
COLLECT = "DELETE FROM {} WHERE day<%s::timestamptz-interval '1 day'"


def inside(cuts):
    """The UTC days lying wholly inside one of the ranges between consecutive cuts."""
    days = []
    for lo, hi in pairwise(cuts):
        day = _floor(lo) if _floor(lo) == lo else _floor(lo) + DAY
        while day + DAY <= hi:
            days.append(day)
            day += DAY
    return days


def split(conn, cuts, hours):
    """(days, hours): the marked days inside the ranges between cuts, which the build reads from
    calculated_geo_days, and the hours of `hours` outside them."""
    wanted = inside(cuts)
    marked = _marked(conn, wanted)
    days = [day for day in wanted if day in marked]
    covered = {day + n * HOUR for day in days for n in range(24)}
    return days, [hour for hour in hours if hour not in covered]


def write(conn, hours):
    """Write and mark each day whose 24 hours are all among `hours` (counted, ascending) and
    that has no mark; returns those days."""
    held = set(hours)
    whole = [day for day in sorted({_floor(h) for h in hours}) if day + 23 * HOUR in held]
    whole = [day for day in whole if all(day + n * HOUR in held for n in range(24))]
    marked = _marked(conn, whole)
    todo = [day for day in whole if day not in marked]
    for day in todo:
        conn.execute(WRITE, {"day": day, "next": day + DAY})
        conn.execute(MARK, (day,))
    return todo


def forget(conn, lo, hi):
    """Delete the days, and their marks, holding an hour of [lo, hi) (counted again)."""
    for table in TABLES:
        conn.execute(FORGET.format(table), {"lo": lo, "hi": hi})


def forget_all(conn):
    for table in TABLES:
        conn.execute(f"DELETE FROM {table}")


def collect(conn, first):
    """Delete the days over a day older than the first hour read."""
    for table in TABLES:
        conn.execute(COLLECT.format(table), (first,))


def _marked(conn, days):
    if not days:
        return set()
    return {row[0] for row in conn.cursor(row_factory=tuple_row).execute(MARKED, (days,))}


def _floor(at):
    return datetime.fromtimestamp(int(at.timestamp() // 86400) * 86400, UTC)
