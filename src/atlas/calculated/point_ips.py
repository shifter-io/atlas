"""The series points' distinct IPs (hourly_windows._count(), CALC-POINT-IPS-1), in bounded
batches and without a sort over their observations.

hourly._values() gives each digest one item (a point is fixed by its runs' clipped buckets), so
a point the 7-day and 30-day series share is counted once per build. BUCKET_IPS takes each
point's distinct IPs (DISTINCT) and counts them per point (count(*)), both hash-aggregated
(hashed(): sorts are disabled for the statement), where count(DISTINCT) sorted every observation
row of every point into temp files on the database server production shares.

The points are cut, in order, into batches of at most calculate.point_rows estimated observation
rows (POINT_ROWS: their runs' successful requests in the point, each giving at most one row, as
parallel_reads.py estimates a group; a larger point is a batch of its own). A lone batch of at
most calculate.heavy_rows is counted on the calculation's connection. Otherwise the batches run
on helper connections in its snapshot (parallel_reads.helpers()), largest first: the ones above
heavy_rows on at most heavy_parallel helpers at heavy_work_mem (the hash of a big batch then
stays in memory), the others at work_mem. The step's batches, time and the temp bytes the
helpers wrote are printed.
"""

from __future__ import annotations

import sys
import time
from collections import deque
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait

import psycopg
from psycopg.rows import dict_row

from . import parallel_reads

# Distinct IPs per series point: the runs' observations in [start, until), each point the
# step-wide bin from start that holds them (series()'s slot()).
BUCKET_IPS = """SELECT slot,at,count(*) AS ips FROM (SELECT DISTINCT k.slot,
 date_bin(k.step,o.bucket_start,k.start) AS at,o.ip_id
FROM unnest(%s::int[],%s::bigint[],%s::timestamptz[],%s::timestamptz[],%s::interval[])
 AS k(slot,run_key,start,until,step)
JOIN ip_observations o ON o.run_key=k.run_key AND o.bucket_start>=k.start
 AND o.bucket_start<k.until) d
GROUP BY 1,2"""
SORTS_OFF = "SELECT current_setting('enable_sort') AS prior,set_config('enable_sort','off',true)"
# Each point's estimated observation rows: its runs' successful requests in [start, until).
POINT_ROWS = """SELECT k.slot,sum(b.successes) AS rows
FROM unnest(%s::int[],%s::bigint[],%s::timestamptz[],%s::timestamptz[])
 AS k(slot,run_key,start,until)
JOIN run_buckets b ON b.run_key=k.run_key AND b.bucket_start>=k.start AND b.bucket_start<k.until
GROUP BY 1"""


def point_ips(conn, items):
    """Each series point's distinct IPs (BUCKET_IPS): {position in items: IPs}."""
    if not items:
        return {}
    found = parallel_reads.settings(conn)
    sizes = dict.fromkeys(range(len(items)), 0)
    estimated = _rows(conn, POINT_ROWS, _columns(items, sizes)[:4])
    sizes |= {row["slot"]: int(row["rows"]) for row in estimated}
    batches = cut(sizes, found["point_rows"])
    if len(batches) == 1 and batches[0][0] <= found["heavy_rows"]:
        return _count(conn, _columns(items, batches[0][1]))
    started, before = time.monotonic(), _temp_bytes(conn)
    counted = _on_helpers(conn, found, [(n, _columns(items, slots)) for n, slots in batches])
    print(
        f"calculate: series point IPs: {len(items)} points in {len(batches)} batches of at most "
        f"{found['point_rows']} rows ({sum(sizes.values())} estimated), "
        f"{time.monotonic() - started:.1f} s, temp written "
        f"{(_temp_bytes(conn) - before) // parallel_reads.MB} MB",
        file=sys.stderr,
    )
    return counted


def cut(sizes, most):
    """[(estimated rows, [slot, ...])]: the slots of sizes ({slot: estimated rows}), in order,
    cut into batches of at most most rows (a larger slot alone), the largest first."""
    batches = []
    for slot, size in sizes.items():
        if not batches or batches[-1][0] + size > most:
            batches.append([0, []])
        batches[-1][0] += size
        batches[-1][1].append(slot)
    return sorted(((rows, slots) for rows, slots in batches), key=lambda batch: -batch[0])


def _columns(items, slots):
    """BUCKET_IPS's columns for the items at slots: a row per run of each."""
    columns = [[] for _ in range(5)]
    for slot in slots:
        item = items[slot]
        for run, *_ in item.clips:
            values = (slot, run, item.since, item.until, item.until - item.since)
            for column, value in zip(columns, values, strict=True):
                column.append(value)
    return columns


def _on_helpers(conn, found, batches):
    """The batches ([(estimated rows, columns)], largest first) counted on helper connections
    in conn's snapshot: the heavy ones (above heavy_rows) on their lane at heavy_work_mem, the
    light ones on the other; without a light helper, the heavy ones count the light ones too. A
    failing batch stops the others."""
    heavy = [columns for rows, columns in batches if rows > found["heavy_rows"]]
    light = [columns for rows, columns in batches if rows <= found["heavy_rows"]]
    h, n = parallel_reads.helpers(conn, found, len(heavy), len(light), "point batches")
    connections = parallel_reads.open_helpers(conn, found, h, n)
    lanes = [(connections[:h], deque(heavy if n else heavy + light))]
    lanes.append((connections[h:], deque(light)))
    counted = {}
    try:
        with ThreadPoolExecutor(h + n, thread_name_prefix="calc-points") as pool:
            futures = [pool.submit(_drain, c, pending) for cs, pending in lanes for c in cs]
            done, _ = wait(futures, return_when=FIRST_EXCEPTION)
            if any(f.exception() for f in done):
                for _, pending in lanes:
                    pending.clear()
            for future in futures:
                counted |= future.result()
    finally:
        for other in connections:
            other.close()
    return counted


def _drain(conn, pending):
    """Count the pending batches on conn until none is left (a deque's popleft is atomic)."""
    counted = {}
    while pending:
        try:
            columns = pending.popleft()
        except IndexError:
            break
        counted |= _count(conn, columns)
    return counted


def _count(conn, columns):
    return {row["slot"]: row["ips"] for row in hashed(conn, BUCKET_IPS, columns)}


def hashed(conn, query, params):
    """query's rows with sorts disabled in conn's transaction for it (SORTS_OFF), so its
    DISTINCT and GROUP BY are hash-aggregated: the planner, which underestimates the joined
    observation rows, would sort them."""
    prior = _rows(conn, SORTS_OFF, ())[0]["prior"]
    found = _rows(conn, query, params)
    conn.execute("SELECT set_config('enable_sort',%s,true)", (prior,))
    return found


def _temp_bytes(conn):
    """The temp file bytes conn's database has written (parallel_reads.TEMP), read on a new
    connection: the helpers report theirs when they close."""
    params = {"password": conn.info.password, "connect_timeout": 5, "autocommit": True}
    with psycopg.connect(conn.info.dsn, **params) as other:
        return other.execute(parallel_reads.TEMP).fetchone()[0]


def _rows(conn, query, params):
    return conn.cursor(row_factory=dict_row).execute(query, params).fetchall()
