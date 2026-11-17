"""The census-pulse 7d and 30d lists rebuilt every 6 hours (CALC-SCALE-4, #1332; the owner's
decision of 17 Nov, Question 2): everything else stays hourly.

A census-pulse group's 7d and 30d windows were counted again every hour (they have no signature),
and their lists (the place and ASN breakdowns and the IP pool lists) grow with the window: the
largest part of the hourly build that grows with the data (#1307). They are now rebuilt by the
first build at or after each of REBUILD_HOURS (UTC); every other build names the list the current
build's window names, when that list was made at or after the latest of those hours, at or before
this build's cutoff, under the same methodology and implementation (carried()). Else, and in a
recount or the full calculation, the window's list is made again. The window's own figures (pool
counts, metrics, scores) are still made every hour; only its list rows can be up to 6 hours old,
and the window says from when (breakdowns_as_of: the cutoff of the build that made its list).
"""

from __future__ import annotations

from datetime import UTC, timedelta

from psycopg.rows import tuple_row

# One hour after the 6-hour marks: away from the first build after midnight, which closes the
# day, and from the 03:00 UTC latency freeze (latency_days.FREEZE_HOUR).
REBUILD_HOURS = (1, 7, 13, 19)
WINDOWS = (168, 720)
# The current build's census-pulse 7d/30d windows' lists made at or after %(since)s, at or
# before %(as_of)s, under this methodology, by the current build of this implementation.
CARRIED = """SELECT w.product_id,w.cohort,w.window_hours,l.list_id,l.entries,
 coalesce(l.figures->'list_entries','{}'::jsonb),b.as_of
FROM current_calculation c JOIN calculated_snapshots s USING(build_id)
JOIN calculated_windows w ON w.build_id=c.build_id AND w.target=''
 AND w.window_hours=ANY(%(windows)s)
JOIN calculated_lists l USING(list_id) JOIN calculation_builds b ON b.build_id=l.created_build
JOIN unnest(%(products)s::int[],%(cohorts)s::text[]) AS k(product_id,cohort)
 ON k.product_id=w.product_id AND k.cohort=w.cohort
WHERE l.signature IS NULL AND b.as_of>=%(since)s AND b.as_of<=%(as_of)s
 AND b.methodology_id=%(method)s AND s.payload->>'implementation_id'=%(implementation)s"""


def since(as_of):
    """The latest of REBUILD_HOURS at or before as_of (UTC): a list made from then on is kept."""
    at = as_of.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    while at.hour not in REBUILD_HOURS or at > as_of:
        at -= timedelta(hours=1)
    return at


def carried(conn, as_of, method_id, implementation_id, groups):
    """{(group, hours): {"list_id", "entries", "pooled", "as_of"}} of the census-pulse groups
    (group keys (product_id, cohort)) whose 7d/30d list the build at as_of keeps."""
    if not groups:
        return {}
    keys = sorted(groups)
    params = {"windows": list(WINDOWS), "since": since(as_of), "as_of": as_of}
    params |= {"products": [k[0] for k in keys], "cohorts": [k[1] for k in keys]}
    params |= {"method": method_id, "implementation": implementation_id}
    rows = conn.cursor(row_factory=tuple_row).execute(CARRIED, params)
    return {
        ((product, cohort), hours): {
            "list_id": list_id,
            "entries": entries,
            "pooled": pooled,
            "as_of": made,
        }
        for product, cohort, hours, list_id, entries, pooled, made in rows
    }
