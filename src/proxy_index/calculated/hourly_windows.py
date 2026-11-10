"""Window counting for hourly.py's memos (contract K6): what a memo miss counts in the database
and the memo tables' reads and writes.

_count() counts the slices and series points (hourly.Item) no memo holds: their pool figures,
first-seen bins (FIRST_SEEN) or distinct IPs per point (point_ips.py). window_distinct() keeps
the estimates' distinct IPs in calculated_distinct. _stored() is the figures a memo keeps and
_restored() a slice's figures from them; _cached() reads a memo table and stores what it misses.
This module never imports hourly.py; hourly.py imports these names back.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .point_ips import BUCKET_IPS as BUCKET_IPS
from .point_ips import point_ips
from .policy import digest
from .poolfig import pool_figures
from .sqlwindow import WindowSpec, distinct_ips, window_inputs

HOUR = timedelta(hours=1)
# IPs per 5-minute bin of their product's first sighting (sqlwindow's bins), of each slot's runs'
# observations in [since, until): no labels, so no enrichment join.
FIRST_SEEN = """SELECT k.slot,
 date_bin('5 minutes',m.first_seen,'1970-01-01 00:00:00+00'::timestamptz) AS bin,count(*) AS ips
FROM (SELECT DISTINCT k.slot,k.product_id,o.ip_id
 FROM unnest(%s::int[],%s::bigint[],%s::int[],%s::timestamptz[],%s::timestamptz[])
  AS k(slot,run_key,product_id,since,until)
 JOIN ip_observations o ON o.run_key=k.run_key AND o.bucket_start>=k.since
  AND o.bucket_start<k.until) k
JOIN product_ips m ON m.product_id=k.product_id AND m.ip_id=k.ip_id GROUP BY 1,2"""
MEMO = "SELECT digest AS key,{column} AS value FROM {table} WHERE digest=ANY(%s)"
# last_used moves at most daily, so a memo read every hour is not rewritten every hour.
TOUCH = (
    "UPDATE {table} SET last_used=now() WHERE digest=ANY(%s) AND last_used<now()-interval '1 day'"
)
SAVE = (
    "INSERT INTO {table}(digest,{column}) SELECT * FROM unnest(%s::text[],%s::jsonb[]) "
    "ON CONFLICT (digest) DO NOTHING"
)
# Prefix of hourly.py's slot and distinct memo digests: its collection deletes only those
# (fleetsql.py keeps its own entries in calculated_distinct).
MEMO_PREFIX = "hourly-v1-"


def window_distinct(conn, run_sets, by_country=True):
    """For each run set of (run_key, country_code) pairs, its distinct IPs by country and in all
    (key None), as estimates.window.distinct() counts them; kept in calculated_distinct."""
    run_sets = [sorted(set(pairs), key=lambda p: (p[0], p[1] or "")) for pairs in run_sets]
    keys = [MEMO_PREFIX + digest(["distinct", by_country, pairs]) for pairs in run_sets]
    wanted = {key: pairs for key, pairs in zip(keys, run_sets, strict=True) if pairs}

    def count(sets):
        return [
            sorted(c.items(), key=lambda i: i[0] or "")
            for c in distinct_ips(conn, sets, by_country)
        ]

    found = _cached(conn, "calculated_distinct", "counts", wanted, count)
    return [{c: n for c, n in found[key]} if key in found else {} for key in keys]


def _count(conn, items):
    """The stored values of slot-memo misses, counted in the database."""
    values, pool = {}, [item for item in items if item.kind == "pool"]
    specs = [_spec(n, item) for n, item in enumerate(pool)]
    for spec, inputs in window_inputs(conn, specs, release_mode="per_run", release_id=None).items():
        item = pool[spec]
        hours = (item.until - item.since) // HOUR
        values[id(item)] = _stored(inputs, item.since, hours, _start(item))
    first = [item for item in items if item.kind == "first_seen"]
    for item, bins in zip(
        first, _first_seen(conn, [_spec(n, i) for n, i in enumerate(first)]), strict=True
    ):
        values[id(item)] = {"first_seen": _bins(bins, _start(item))}
    ips = [item for item in items if item.kind == "ips"]
    counted = point_ips(conn, ips)
    for n, item in enumerate(ips):
        values[id(item)] = {"unique_ips": counted.get(n, 0)}
    return [values[id(item)] for item in items]


def _spec(n, item):
    runs = tuple(run for run, *_ in item.clips)
    return WindowSpec(n, item.product, runs, item.since, item.until, item.asked)


def _start(item):
    """The earliest start of a slice as long as item that holds its clipped bounds: bins before
    it are never new IPs of a slice using the stored value."""
    return min(first for _, first, _ in item.clips) - (item.until - item.since)


def _stored(inputs, since, hours, start):
    """A window's pool figures as a memo stores them: without its new IPs (they depend on the
    slice start) and with the first-seen bins from start on."""
    figures = pool_figures(inputs, since, hours)
    del figures["new_ips"], figures["churn_rate"]
    return figures | {"first_seen": _bins(inputs.first_seen, start)}


def _bins(bins, start):
    return {at.isoformat(): n for at, n in sorted(bins.items()) if at >= start}


def _restored(stored, item):
    """A slice's figures from its stored value (_stored()): new IPs from since on."""
    bins = {}
    for at, n in stored["first_seen"].items():
        if (at := datetime.fromisoformat(at)) >= item.since:
            bins[at] = n
    if item.kind == "first_seen":
        return {"first_seen": bins}
    size = stored["unique_ips"]
    if not size:
        return None
    new = sum(bins.values())
    return stored | {"new_ips": new, "churn_rate": new / size, "first_seen": bins}


def _first_seen(conn, specs):
    """Each spec's first-seen bins (FIRST_SEEN): [{bin start: IPs}] in specs' order."""
    columns = [[] for _ in range(5)]
    for slot, spec in enumerate(specs):
        for run_key in sorted(spec.runs):
            values = (slot, run_key, spec.product_id, spec.since, spec.until)
            for column, value in zip(columns, values, strict=True):
                column.append(value)
    bins = [{} for _ in specs]
    if columns[0]:
        for row in _rows(conn, FIRST_SEEN, columns):
            bins[row["slot"]][row["bin"]] = row["ips"]
    return bins


def _cached(conn, table, column, wanted, compute):
    """{digest: value} of the memo table's entries for wanted ({digest: input}), computing and
    storing the missing ones (compute(inputs) gives their JSON values in order)."""
    if not wanted:
        return {}
    keys = sorted(wanted)
    query = MEMO.format(column=column, table=table)
    found = {row["key"]: row["value"] for row in _rows(conn, query, (keys,))}
    if found:
        conn.execute(TOUCH.format(table=table), (sorted(found),))
    missing = [key for key in keys if key not in found]
    if missing:
        values = compute([wanted[key] for key in missing])
        conn.execute(SAVE.format(table=table, column=column), (missing, [Jsonb(v) for v in values]))
        found |= dict(zip(missing, values, strict=True))
    return found


def _rows(conn, query, params):
    return conn.cursor(row_factory=dict_row).execute(query, params).fetchall()
