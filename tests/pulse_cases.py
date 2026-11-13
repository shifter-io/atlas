"""Shared helpers of the pulse tests: evaluation windows, pulse runs with set buckets, the tape.

Database tests: load the fixtures with `pytest_plugins = ["test_storage", "orchestrator_cases"]`.
"""

from datetime import UTC, datetime, timedelta

from census_pulse_cases import batch, batch_run, product
from orchestrator_cases import query
from test_storage import measurement

from atlas.orchestrator import pulse
from atlas.storage import connect, ingest, partition

W0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
WINDOW = pulse.WINDOW
SETTLE = timedelta(seconds=180)


def window(k):
    return W0 + WINDOW * k


async def evaluated(db, k):
    """Evaluate up to window k, the moment it has settled."""
    return await pulse.evaluate(db, window(k) + WINDOW + SETTLE)


def buckets(conn, key, rows):
    """Replace the run's buckets with (start, completed, errors, p50 ms) rows."""
    conn.execute("DELETE FROM run_buckets WHERE run_key=%s", (key,))
    for start, completed, errors, p50 in rows:
        partition(conn, "run_buckets", start)
        conn.execute(
            "INSERT INTO run_buckets(bucket_start,run_key,completed,successes,"
            "response_body_bytes,errors,status_codes,histogram_steps,histogram_counts,timeouts) "
            "VALUES (%s,%s,%s,%s,0,'{}','{}',%s,%s,%s)",
            (start, key, completed, completed - errors, [p50], [completed - errors or 1], errors),
        )


def products(database, *names):
    with connect(database) as conn:
        return [product(conn, name) for name in names]


async def run(tmp_path, database, kind, at, network, country, rows, target="generic_http"):
    """A done batch of `kind` with one run of the network whose buckets are `rows`."""
    directory = await measurement(tmp_path / f"{kind}-{country}-{at:%H%M}-{network}")
    key = ingest(directory, database)["run_key"]
    with connect(database) as conn:
        buckets(conn, key, rows)
        batch_id = batch(conn, kind, at, country=country, target=target)
        batch_run(conn, batch_id, network, at + timedelta(minutes=1), run_key=key)
    return key


def tape(database, kind):
    return query(
        database,
        "SELECT product_id,data FROM test_events WHERE kind=%s ORDER BY event_id",
        (kind,),
    )
