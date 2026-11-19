"""A lost browser run's own late upload counts, also once the planner queued it again
(orchestrator/browser_ingest.py, BROWSER-COVERAGE-1, #1392): its node finished it cut off from the
manager, the reaper failed it, the planner queued its batch again (browser_rerun), and the node's
bundle then arrives. Before, the upload was refused ("run is queued") and its result was lost."""

import pytest
from browser_ingest_cases import browser_run, make_bundle, run_rows, upload
from orchestrator_cases import query

from atlas.storage import connect

# The fixtures (db, client, scratch, world) are browser_ingest_cases'.
pytest_plugins = ["test_storage", "browser_ingest_cases"]


@pytest.mark.parametrize("error", ["not_started", "stale"])
async def test_a_late_upload_into_a_batch_queued_again_is_its_result(
    database, client, world, scratch, error
):
    batch_id = browser_run(database, "alpha")
    with connect(database) as conn:  # failed by the reaper, then queued again by the planner
        conn.execute(
            "UPDATE batch_runs SET status='queued',error=%s,started_at=NULL WHERE batch_id=%s",
            (error, batch_id),
        )
        conn.execute(
            "UPDATE test_batches SET status='scheduled',start_at=start_at+interval '1 hour' "
            "WHERE batch_id=%s",
            (batch_id,),
        )
    data, run_id = await make_bundle(world["tmp"], batch_id)
    status, body, _ = await upload(client, world["tokens"]["alpha"], batch_id, data, run_id)
    assert status == 200 and body["status"] == "done", body
    assert run_rows(database, batch_id)[0][:2] == ("done", body["run_key"])
    assert query(database, "SELECT status FROM test_batches WHERE batch_id=%s", (batch_id,)) == [
        ("done",)
    ]


async def test_a_run_queued_again_takes_an_upload_only_from_its_own_node(
    database, client, world, scratch
):
    """Another node's upload for it is refused as before; a run queued for the first time (no
    reaper error) is still a conflict (test_browser_ingest_runs)."""
    batch_id = browser_run(database, "alpha", status="queued")
    query(
        database,
        "UPDATE batch_runs SET error='not_started' WHERE batch_id=%s RETURNING 1",
        (batch_id,),
    )
    data, run_id = await make_bundle(world["tmp"], batch_id)
    status, _, _ = await upload(client, world["tokens"]["beta"], batch_id, data, run_id)
    assert status == 404
    assert run_rows(database, batch_id)[0][:2] == ("queued", None)
