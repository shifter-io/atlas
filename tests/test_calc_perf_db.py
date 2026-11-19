"""CALC-PERF-1's database changes, none of which changes an answer: the import's transaction
runs without JIT, traffic.summary() reads each run's buckets inside the run's own span, and the
geo hours of runs outside a batch have a partial index that rollups.STALE reads.
"""

import psycopg
import pytest
from census_pulse_cases import scenario
from storage_cases import enrichment, measurement

from atlas import traffic
from atlas.calculated import rollups, store
from atlas.storage import connect, ingest

pytest_plugins = ["test_storage"]

# The run buckets outside their run's span (the operator's check before the bound is deployed:
# it must find none, so the bounded lookup sums exactly what the unbounded one did).
OUTSIDE = (
    "SELECT count(*) FROM run_buckets b JOIN test_runs r USING(run_key) "
    "WHERE b.bucket_start<date_bin('5 minutes',r.started_at,'1970-01-01 00:00:00+00'::timestamptz) "
    "OR b.bucket_start>r.finished_at"
)


async def test_an_import_runs_without_jit(database, tmp_path, monkeypatch):
    sent = []
    original = psycopg.Connection.execute

    def execute(self, query, *args, **kwargs):
        sent.append(query)
        return original(self, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Connection, "execute", execute)
    directory = await measurement(tmp_path)
    enrichment(directory)
    assert ingest(directory, database)["imported"]
    assert sent[sent.index("BEGIN") + 1] == "SET LOCAL jit=off"


@pytest.mark.asyncio
async def test_traffic_reads_each_run_inside_its_span(database, tmp_path):
    await scenario(database, tmp_path)
    with connect(database) as conn:
        assert conn.execute(OUTSIDE).fetchone()[0] == 0
        report = traffic.summary(conn, traffic.TrafficInputs())
        # A run whose span moved off its buckets has no artifact sums: the bound is in force.
        conn.execute(
            "UPDATE test_runs SET started_at=finished_at+interval '1 hour',"
            "finished_at=finished_at+interval '1 hour' WHERE run_key=(SELECT min(run_key) "
            "FROM batch_runs JOIN run_buckets USING(run_key))"
        )
        moved = traffic.summary(conn, traffic.TrafficInputs())
    artifacts = [sum(r["artifact_runs"] for r in found["rows"]) for found in (report, moved)]
    assert artifacts[0] > 0 and artifacts[1] == artifacts[0] - 1


def test_the_stale_check_reads_the_geo_hours_of_runs_outside_a_batch_by_index(database):
    store.initialize(database)
    with connect(database) as conn:
        found = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE indexname='calculated_geo_hours_run'"
        ).fetchone()
    assert found and found[0].endswith("(run_key) WHERE (run_key IS NOT NULL)")
    assert "h.run_key IS NOT NULL AND r.batch_id IS NOT NULL" in rollups.STALE
