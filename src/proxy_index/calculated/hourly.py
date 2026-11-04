"""Window values that move with the cutoff (contract K6; CALC-INC-2 replaces the bodies).

A stored list holds what a window's runs fix; these change every hour even when the runs do not:
new IPs (new_ips()), pool counts of time slices (slice_figures(): the daily pools and each
window's range), distinct IPs per series point (bucket_ips()), the estimates' distinct IPs
(window_distinct()) and the lists a newly processed run makes stale (stale_lists()). The
full-mode bodies count them in the database from the observations; nothing per IP comes back.
"""

from __future__ import annotations

from datetime import timedelta

from psycopg.rows import dict_row

from .poolfig import pool_figures
from .sqlwindow import WindowSpec, distinct_ips, window_inputs

HOUR = timedelta(hours=1)
# Distinct IPs per series point: the runs' observations in [start, until), each point the
# step-wide bin from start that holds them (series()'s slot()).
BUCKET_IPS = """SELECT k.slot,date_bin(k.step,o.bucket_start,k.start) AS at,
 count(DISTINCT o.ip_id) AS ips
FROM unnest(%s::int[],%s::bigint[],%s::timestamptz[],%s::timestamptz[],%s::interval[])
 AS k(slot,run_key,start,until,step)
JOIN ip_observations o ON o.run_key=k.run_key AND o.bucket_start>=k.start
 AND o.bucket_start<k.until
GROUP BY 1,2"""


def new_ips(figures, since):
    """IPs of a window first seen by its product at or after since (on the 5-minute grid):
    the bins of figures["first_seen"] (slice_figures() of the window's range) from since on."""
    return sum(n for start, n in (figures or {}).get("first_seen", {}).items() if start >= since)


def slice_figures(conn, group_runs, slices):
    """Pool counts of each slice of each group: {key: [figures or None, ...]} in slices' order.

    group_runs maps a key to (product_id, {run_key: requested country}); slices maps it to its
    [(since, until), ...] on the 5-minute grid. A slice's figures are poolfig.pool_figures() of
    its observations (each run labelled by its own newest release, as the calculator reads them;
    shares with the default hosting usage types) plus "first_seen", the IPs per 5-minute bin of
    the product's first sighting; a slice without observations is None, as daily_pools() has it.
    """
    specs = [
        WindowSpec((key, n), product, tuple(asked), since, until, asked)
        for key, (product, asked) in group_runs.items()
        for n, (since, until) in enumerate(slices.get(key, ()))
    ]
    found = window_inputs(conn, specs, release_mode="per_run", release_id=None)
    result = {key: [None] * len(slices.get(key, ())) for key in group_runs}
    for spec in specs:
        inputs = found[spec.key]
        if inputs.unique_ips:
            hours = (spec.until - spec.since) // HOUR
            figures = pool_figures(inputs, spec.since, hours) | {"first_seen": inputs.first_seen}
            result[spec.key[0]][spec.key[1]] = figures
    return result


def bucket_ips(conn, group_runs, buckets):
    """Distinct IPs of each group's runs per series point: {key: {point start: IPs}}.

    group_runs maps a key to its run keys, buckets to (start, until, step): the points are the
    step-wide bins from start (5 minutes for a 24-hour series, else days); a point without
    observations is absent."""
    columns = [[] for _ in range(5)]
    keys = list(buckets)
    for slot, key in enumerate(keys):
        start, until, step = buckets[key]
        for run_key in sorted(set(group_runs.get(key, ()))):
            for column, value in zip(columns, (slot, run_key, start, until, step), strict=True):
                column.append(value)
    result = {key: {} for key in keys}
    if columns[0]:
        for row in conn.cursor(row_factory=dict_row).execute(BUCKET_IPS, columns):
            result[keys[row["slot"]]][row["at"]] = row["ips"]
    return result


def window_distinct(conn, run_sets, by_country=True):
    """For each run set of (run_key, country_code) pairs, its distinct IPs by country and in all
    (key None), as estimates.window.distinct() counts them."""
    return distinct_ips(conn, run_sets, by_country)


def stale_lists(conn, new_runs):
    """List ids whose stored figures the newly processed runs (new_runs) make stale, so their
    windows are counted again. The stored figures hold nothing that moves with the cutoff yet
    (new IPs come from slice_figures() every build), so none is stale. CALC-INC-2 stores each
    list's first-seen histogram and applies its late-run rule here."""
    return frozenset()
