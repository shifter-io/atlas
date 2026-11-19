"""A browser run the reaper failed while its node was silent may still finish
(orchestrator/nodework.py, _finishing, BROWSER-COVERAGE-1, #1392): the hosts lost the manager for
88 minutes, the reaper failed their runs as stale, and when their progress came back with all
tries done they were told to stop and deleted. Now such a run (stale, or never seen to start) is
not told to stop before its deadline and its progress is kept, so its node finishes and uploads
it (a late upload, browser_ingest.REAPED); past the deadline, or once killed, it stops."""

from datetime import timedelta

import pytest
from browser_queue_cases import DAY, networks, queue_document
from orchestrator_cases import prepare, query, set_setting
from test_browser_settings import write

from atlas.browser.settings import load_settings
from atlas.orchestrator import browser, nodework
from atlas.orchestrator.browser import browser_plan
from atlas.orchestrator.browser_queue import EPOCH, queues
from atlas.orchestrator.db import Database
from atlas.orchestrator.nodes import issue_token

pytest_plugins = ["test_storage"]

LEAD = timedelta(seconds=60)
D = (DAY - EPOCH) // timedelta(days=1)
MINUTE = timedelta(minutes=1)
RUN = "SELECT status,error,requests,last_progress_at FROM batch_runs WHERE batch_id=%s"


@pytest.fixture
async def db(database):
    pool = Database(database)
    yield pool
    await pool.close()


@pytest.fixture(autouse=True)
def fresh_cache():
    browser._LOADED.clear()
    yield
    browser._LOADED.clear()


async def opened(database, db, tmp_path):
    """browser-1's first entry opened and claimed by its node, the settings file in use (its
    deadline: 120 minutes)."""
    prepare(database)
    nodes = {}
    for network in networks(1):
        provider, product = network.split("/")
        nodes[network] = issue_token(
            database, provider=provider, product=product, region="browser", host="browser-1"
        )
    doc = queue_document()
    doc["schedule"]["run_deadline_minutes"] = 120
    path = write(tmp_path, doc)
    await set_setting(db, "browser.settings_file", str(path))
    settings = load_settings(path)
    entry = queues(settings)[0].entries(D, LEAD)[0]
    assert (await browser_plan(db, entry.start - 2 * MINUTE, settings))["created"] == 1
    node = nodes[entry.network]
    await nodework.assignments(db, node, entry.start)
    batch = query(database, "SELECT batch_id FROM test_batches")[0][0]
    return node, batch, entry.start


async def test_a_stale_run_whose_node_comes_back_may_finish_before_its_deadline(
    database, db, tmp_path
):
    node, batch, t = await opened(database, db, tmp_path)
    assert await nodework.progress(db, node, batch, {"requests": 40}, t + MINUTE) == {"stop": False}
    # The host loses the manager: no progress for 30 minutes, the reaper fails the run.
    assert (await nodework.reap(db, t + 32 * MINUTE))["failed"] == {"stale": 1}
    # Back after 88 minutes with every try done: it goes on, its progress kept.
    back = t + 90 * MINUTE
    reply = await nodework.progress(db, node, batch, {"requests": 100}, back)
    assert reply == {"stop": False}
    assert query(database, RUN, (batch,)) == [("failed", "stale", 100, back)]
    # Past its deadline (start + 120 minutes) it is told to stop.
    late = await nodework.progress(db, node, batch, {"requests": 100}, t + 121 * MINUTE)
    assert late == {"stop": True}


async def test_a_run_never_seen_to_start_may_finish_too(database, db, tmp_path):
    node, batch, t = await opened(database, db, tmp_path)
    # Claimed, then cut off before its first progress: failed as never started.
    assert (await nodework.reap(db, t + 4 * MINUTE))["failed"] == {"not_started": 1}
    reply = await nodework.progress(db, node, batch, {"requests": 7}, t + 60 * MINUTE)
    assert reply == {"stop": False}
    assert query(database, RUN, (batch,)) == [("failed", "not_started", 7, t + 60 * MINUTE)]


async def test_a_killed_run_still_stops(database, db, tmp_path):
    node, batch, t = await opened(database, db, tmp_path)
    await nodework.progress(db, node, batch, {"requests": 5}, t + MINUTE)
    query(
        database,
        "UPDATE batch_runs SET status='killed',finished_at=%s WHERE batch_id=%s RETURNING 1",
        (t + 2 * MINUTE, batch),
    )
    reply = await nodework.progress(db, node, batch, {"requests": 6}, t + 3 * MINUTE)
    assert reply == {"stop": True}
    assert query(database, RUN, (batch,))[0][:3] == ("killed", None, 5)
