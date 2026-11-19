"""Lost browser runs run again (orchestrator/browser_rerun.py, BROWSER-COVERAGE-1, #1392): an
entry whose run its node never started is caught up once the node is online, at once when its
host has a free place (else at its next free position), its batch reopened; one that can no
longer finish before its next start stays failed; a run that started and went stale is run again
only once its deadline passed with nothing uploaded (before, its node may still finish it)."""

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
BATCH = (
    "SELECT b.batch_id,b.status,b.start_at,br.status,br.error FROM test_batches b "
    "JOIN batch_runs br USING(batch_id) WHERE b.kind='browser' AND b.scheduled_for=%s"
)


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


def setup(database, tmp_path):
    """browser-1's 6 networks with their nodes (offline until set online), its entries of day D."""
    prepare(database)
    nodes = {}
    for network in networks(1):
        provider, product = network.split("/")
        nodes[network] = issue_token(
            database, provider=provider, product=product, region="browser", host="browser-1"
        )
    settings = load_settings(write(tmp_path, queue_document()))
    return settings, queues(settings)[0].entries(D, LEAD), nodes


def online(database, node):
    query(
        database,
        "UPDATE nodes SET status='online' WHERE node_id=%s RETURNING 1",
        (node["node_id"],),
    )


async def lost(db, database, settings, entry):
    """The entry opened on time and failed by the reaper as never started (the host cut off)."""
    assert (await browser_plan(db, entry.start - 2 * MINUTE, settings))["created"] == 1
    reaped = await nodework.reap(db, entry.start + 4 * MINUTE)
    assert reaped == {"failed": {"not_started": 1}, "closed": 1}
    return query(database, BATCH, (entry.start,))[0][0]


async def test_a_run_never_started_runs_again_once_its_node_is_online(database, db, tmp_path):
    settings, entries, nodes = setup(database, tmp_path)
    first, second = entries[0], entries[1]
    batch = await lost(db, database, settings, first)
    # Its node is still offline: nothing is run again yet.
    offline = await browser_plan(db, first.start + 10 * MINUTE, settings)
    assert "rerun" not in offline and offline["created"] == 0
    assert query(database, BATCH, (first.start,)) == [
        (batch, "failed", first.start, "failed", "not_started")
    ]
    # Back online as the next entry opens: the host has a free place, so the lost run starts at
    # once, beside that entry's own run on another network.
    online(database, nodes[first.network])
    now = second.start - 2 * MINUTE
    result = await browser_plan(db, now, settings)
    assert (result["rerun"], result["caught_up"], result["missed"]) == (1, 0, 0)
    assert result["opened"] == [second.target] and second.network != first.network
    assert query(database, BATCH, (first.start,)) == [
        (batch, "scheduled", now, "queued", "not_started")
    ]
    # Its node is handed it for the new start; it is decided once.
    work = await nodework.assignments(db, nodes[first.network], now)
    assert [w["batch_id"] for w in work["assignments"]] == [batch]
    assert work["assignments"][0]["start_at"] == now.isoformat()
    again = await browser_plan(db, now + MINUTE, settings)
    assert "rerun" not in again and again["created"] == 0


async def test_a_lost_run_that_cannot_finish_in_time_stays_failed(database, db, tmp_path):
    settings, entries, nodes = setup(database, tmp_path)
    first = entries[0]
    batch = await lost(db, database, settings, first)
    online(database, nodes[first.network])
    # Back 21 h 30 min later: a run starting now would not end (3 h) before tomorrow's turn.
    result = await browser_plan(db, first.start + timedelta(hours=21, minutes=30), settings)
    assert "rerun" not in result
    assert query(database, BATCH, (first.start,)) == [
        (batch, "failed", first.start, "failed", "not_started")
    ]
    # Not skipped again either: its failed batch is the entry's only one.
    assert query(
        database,
        "SELECT count(*) FROM test_batches WHERE scheduled_for=%s",
        (first.start,),
    ) == [(1,)]


async def test_a_run_that_started_and_went_stale_is_not_run_again(database, db, tmp_path):
    settings, entries, nodes = setup(database, tmp_path)
    first, second = entries[0], entries[1]
    node = nodes[first.network]
    online(database, node)
    assert (await browser_plan(db, first.start - 2 * MINUTE, settings))["created"] == 1
    await nodework.assignments(db, node, first.start)
    batch = query(database, BATCH, (first.start,))[0][0]
    await nodework.progress(db, node, batch, {"requests": 5}, first.start + MINUTE)
    reaped = await nodework.reap(db, first.start + 32 * MINUTE)
    assert reaped == {"failed": {"stale": 1}, "closed": 1}
    result = await browser_plan(db, second.start + 30 * MINUTE, settings)
    assert "rerun" not in result
    assert query(database, BATCH, (first.start,))[0][1:] == (
        "failed",
        first.start,
        "failed",
        "stale",
    )


async def test_a_stale_run_never_uploaded_runs_again_past_its_deadline(database, db, tmp_path):
    """BROWSER-COVERAGE-1: a run that went silent mid-run (or whose upload never completed) is
    lost once its deadline passed with nothing uploaded: it runs again, its counters cleared."""
    _, _, nodes = setup(database, tmp_path)
    doc = queue_document()
    doc["schedule"]["run_deadline_minutes"] = 120
    folder = tmp_path / "deadline"  # beside setup()'s own settings file
    folder.mkdir()
    path = write(folder, doc)
    await set_setting(db, "browser.settings_file", str(path))
    settings = load_settings(path)
    first = queues(settings)[0].entries(D, LEAD)[0]
    node = nodes[first.network]
    online(database, node)
    assert (await browser_plan(db, first.start - 2 * MINUTE, settings))["created"] == 1
    await nodework.assignments(db, node, first.start)
    batch = query(database, BATCH, (first.start,))[0][0]
    await nodework.progress(db, node, batch, {"requests": 40}, first.start + MINUTE)
    assert (await nodework.reap(db, first.start + 32 * MINUTE))["failed"] == {"stale": 1}
    # Before its deadline its node may still finish and upload it: not run again.
    early = await browser_plan(db, first.start + 60 * MINUTE, settings)
    assert "rerun" not in early
    # Past it with nothing uploaded: queued again, its start, counters and progress cleared.
    now = first.start + 121 * MINUTE
    assert (await browser_plan(db, now, settings))["rerun"] == 1
    row = query(
        database,
        "SELECT b.status,b.start_at>%s,br.status,br.error,br.started_at,br.requests,"
        "br.last_progress_at FROM test_batches b JOIN batch_runs br USING(batch_id) "
        "WHERE b.batch_id=%s",
        (now - MINUTE, batch),
    )
    assert row == [("scheduled", True, "queued", "stale", None, 0, None)]
