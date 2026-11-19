"""A coverage gap names the failed run behind it (orchestrator/coverage.py, BROWSER-COVERAGE-1,
#1392): the 19 Nov pages listed 12 networks, one line each, for one cause (the three hosts cut
off from the manager for 88 minutes). Now each browser gap whose newest run since its last
counted one failed says when, on which host and why, and the summary counts them per host."""

from __future__ import annotations

import pytest
from orchestrator_cases import query
from test_orchestrator_coverage import ALERTS, HOUR, NOW, stock

from atlas.orchestrator.coverage import SUMMARY, Gap, coverage, hosts_text
from atlas.orchestrator.db import Database
from atlas.orchestrator.nodes import BROWSER_REGION_ROW, issue_token
from atlas.storage import connect

pytest_plugins = ["test_storage"]


@pytest.fixture
async def db(database):
    pool = Database(database)
    yield pool
    await pool.close()


def failed(database, product, target, slot, error, host="browser-2"):
    """A failed browser batch of the network's node on `host` at `slot`."""
    node = issue_token(
        database, provider="alpha", product="residential", region="browser", host=host
    )
    with connect(database) as conn:
        batch = conn.execute(
            "INSERT INTO test_batches(kind,country_code,region_id,target_id,scheduled_for,"
            "start_at,status) VALUES ('browser','US',%s,%s,%s,%s,'failed') RETURNING batch_id",
            (BROWSER_REGION_ROW[0], target, slot, slot),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO batch_runs(batch_id,product_id,node_id,status,error) "
            "VALUES (%s,%s,%s,'failed',%s)",
            (batch, product, node["node_id"], error),
        )


async def test_a_gap_names_its_failed_run_and_the_summary_its_host(database, db, tmp_path):
    ages = lambda t: [40, 40] + [2] * (len(t) - 2)  # noqa: E731
    root, targets, alpha = await stock(database, db, tmp_path, ages)
    # The first pair's newest failed run is after its last counted run (40 h ago): named. The
    # second's is before it: not this gap's cause.
    failed(database, alpha, targets[0], NOW - 20 * HOUR, "not_started")
    failed(database, alpha, targets[1], NOW - 50 * HOUR, "stale")
    found = await coverage(db, root, NOW)
    words = (
        f"browser target {targets[0]} has no counted run for 40 hours (its run of "
        "2026-12-31 16:00 UTC on browser-2 failed: never started)"
    )
    plain = f"browser target {targets[1]} has no counted run for 40 hours"
    assert found["missing"] == {"alpha": [words, plain]}
    [summary] = query(database, ALERTS, (SUMMARY,))
    assert "Their last runs failed on browser-2 (1, 2026-12-31 16:00 UTC). " in summary[1]
    assert f"alpha: {words}; {plain}." in summary[1]


def test_the_summary_counts_failed_runs_per_host_the_most_first():
    def gap(network, host, slot, error="stale"):
        return Gap(network, "browser", "x_web", 46, (host, slot, error))

    found = [
        gap("a", "browser-1", "2026-11-18 00:25"),
        gap("b", "browser-2", "2026-11-18 01:22", "deadline"),
        gap("c", "browser-2", "2026-11-17 22:49"),
        Gap("d", "pulses", None, 4),
    ]
    assert hosts_text(found) == (
        "Their last runs failed on browser-2 (2, 2026-11-17 22:49 to 2026-11-18 01:22 UTC), "
        "browser-1 (1, 2026-11-18 00:25 UTC). "
    )
    assert hosts_text(found[3:]) == ""
    assert found[1].words().endswith("failed: past its deadline)")
    assert Gap("e", "browser", "x_web", None, ("-", "2026-11-18 00:25", "stalled")).words() == (
        "browser target x_web has no counted run in the last 8 days (its run of 2026-11-18 "
        "00:25 UTC on - failed: stalled)"
    )
