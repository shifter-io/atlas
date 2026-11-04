"""Census and pulse inputs of the incremental calculation (contract K6; CALC-PULSE-1 replaces the
bodies with per-hour rollups): full-mode bodies run today's queries.
"""

from __future__ import annotations

from psycopg.rows import dict_row

# A product's batch-run buckets in [start, as_of), in the columns and order the calculator reads.
SCHEDULED_ROWS = (
    "SELECT b.bucket_start,b.run_key,run_bucket_counters(b) AS counters,"
    "b.outcome_counters AS outcome FROM run_buckets b JOIN test_runs r USING(run_key) "
    "JOIN scope_stats s ON s.scope_id=r.scope_id WHERE r.batch_id IS NOT NULL "
    "AND s.product_id=%s AND b.bucket_start>=%s AND b.bucket_start<%s "
    "ORDER BY b.bucket_start,b.run_key"
)


def scheduled_inputs(conn, as_of, timed_target):
    """The census/pulse history of the 30-day window (calculator.scheduled_inputs())."""
    from .calculator import scheduled_inputs as today  # calculator imports this module

    return today(conn, as_of, timed_target)


def scheduled_rows(conn, product_id, start, as_of):
    """The request buckets of the product's batch runs in [start, as_of)."""
    return (
        conn.cursor(row_factory=dict_row)
        .execute(SCHEDULED_ROWS, (product_id, start, as_of))
        .fetchall()
    )
