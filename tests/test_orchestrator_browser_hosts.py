"""A browser host's run limit and stuck runs (orchestrator/browser_hosts.py, BROWSER-HOST-LIMIT-1):
the queue planner never opens past browsers_per_host, last_progress_at moves only with the
counters, and the reaper abandons a browser run without progress or past its deadline."""

from dataclasses import replace
from datetime import timedelta

import pytest
from browser_queue_cases import DAY, networks, queue_document
from orchestrator_cases import prepare, query, set_setting
from test_browser_settings import write

from atlas.browser.settings import load_settings
from atlas.orchestrator import browser, nodework
from atlas.orchestrator.browser import browser_plan
from atlas.orchestrator.browser_hosts import ABANDONED_ALERT, OVER_ALERT
from atlas.orchestrator.browser_queue import EPOCH, queues
from atlas.orchestrator.db import Database
from atlas.orchestrator.nodes import issue_token

pytest_plugins = ["test_storage"]

PERIOD = timedelta(days=1)
LEAD = timedelta(seconds=60)
D = (DAY - EPOCH) // PERIOD
MINUTE = timedelta(minutes=1)
ALERTS = "SELECT dedupe_key,message,occurrences,acknowledged_at IS NULL FROM alerts WHERE kind=%s"


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


def setup(database, tmp_path, **schedule):
    """browser-1's 6 networks with their nodes; its queue entries of day D; the settings file."""
    prepare(database)
    nodes = {}
    for network in networks(1):
        provider, product = network.split("/")
        nodes[network] = issue_token(
            database, provider=provider, product=product, region="browser", host="browser-1"
        )
    doc = queue_document()
    doc["schedule"].update(schedule)
    path = write(tmp_path, doc)
    settings = load_settings(path)
    return settings, queues(settings)[0].entries(D, PERIOD, LEAD), nodes, path


async def started(db, database, settings, nodes, entry):
    """The entry's batch opened two minutes ahead, then claimed and started by its node."""
    assert (await browser_plan(db, entry.start - 2 * MINUTE, settings))["created"] == 1
    node = nodes[entry.network]
    await nodework.assignments(db, node, entry.start)
    batch = query(
        database, "SELECT batch_id FROM test_batches WHERE scheduled_for=%s", (entry.start,)
    )[0][0]
    assert await nodework.progress(db, node, batch, {"requests": 0}, entry.start) == {"stop": False}
    return node, batch


def run(database, batch):
    return query(
        database,
        "SELECT status,error,last_progress_at FROM batch_runs WHERE batch_id=%s",
        (batch,),
    )[0]


async def test_a_full_host_opens_nothing_until_a_run_ends(database, db, tmp_path):
    settings, entries, nodes, _ = setup(database, tmp_path)
    _, first = await started(db, database, settings, nodes, entries[0])
    await started(db, database, settings, nodes, entries[1])
    # browsers_per_host = 2 runs are open on browser-1: the third entry waits.
    full = await browser_plan(db, entries[2].start - 2 * MINUTE, settings)
    assert (full["created"], full["waiting"]) == (0, 1)
    assert query(database, ALERTS, (OVER_ALERT,)) == []
    query(database, "UPDATE batch_runs SET status='done' WHERE batch_id=%s RETURNING 1", (first,))
    freed = await browser_plan(db, entries[2].start - MINUTE, settings)
    assert freed["opened"] == [entries[2].target] and "waiting" not in freed


async def test_a_host_over_its_limit_alerts_until_it_is_back_under(database, db, tmp_path):
    settings, entries, nodes, _ = setup(database, tmp_path)
    _, first = await started(db, database, settings, nodes, entries[0])
    await started(db, database, settings, nodes, entries[1])
    one = replace(settings, browser=replace(settings.browser, browsers_per_host=1))
    for minute in (4, 3):
        result = await browser_plan(db, entries[2].start - minute * MINUTE, one)
        assert result["created"] == 0
    message = "browser-1 has 2 browser runs open, limit 1"
    assert query(database, ALERTS, (OVER_ALERT,)) == [(f"{OVER_ALERT}:browser-1", message, 2, True)]
    query(database, "UPDATE batch_runs SET status='done' WHERE batch_id=%s RETURNING 1", (first,))
    await browser_plan(db, entries[2].start - 2 * MINUTE, one)
    assert query(database, ALERTS, (OVER_ALERT,))[0][2:] == (2, False)


async def test_progress_moves_only_when_the_counters_do(database, db, tmp_path):
    settings, entries, nodes, _ = setup(database, tmp_path)
    node, batch = await started(db, database, settings, nodes, entries[0])
    assert run(database, batch)[2] is None  # started, nothing counted yet
    t = entries[0].start
    counters = {"requests": 5, "bytes_down": 100}
    for minute in (1, 2, 3):
        await nodework.progress(db, node, batch, counters, t + minute * MINUTE)
    assert run(database, batch)[2] == t + MINUTE
    await nodework.progress(db, node, batch, counters | {"bytes_down": 200}, t + 4 * MINUTE)
    assert run(database, batch)[2] == t + 4 * MINUTE


async def test_a_browser_run_without_progress_is_abandoned_after_stall_minutes(
    database, db, tmp_path
):
    settings, entries, nodes, _ = setup(database, tmp_path)
    node, batch = await started(db, database, settings, nodes, entries[0])
    t = entries[0].start
    await nodework.progress(db, node, batch, {"requests": 99}, t + MINUTE)
    # Silent for 20 minutes, past batch.stale_seconds (15): still a browser run's to finish.
    await nodework.progress(db, node, batch, {"requests": 99}, t + 21 * MINUTE)
    assert await nodework.reap(db, t + 21 * MINUTE) == {"failed": {}, "closed": 0}
    assert await nodework.reap(db, t + 32 * MINUTE) == {"failed": {"stale": 1}, "closed": 1}
    assert run(database, batch)[:2] == ("failed", "stale")
    message = f"browser-1: browser run {batch} (ai_chatgpt) made no progress; abandoned"
    assert query(database, ALERTS, (ABANDONED_ALERT,)) == [
        (f"{ABANDONED_ALERT}:browser-1:2026-11-08", message, 1, True)
    ]
    assert query(database, "SELECT count(*) FROM alerts") == [(1,)]  # no run_stale beside it
    # The node is told to stop; the host's place is free for the next entry.
    reply = await nodework.progress(db, node, batch, {"requests": 99}, t + 33 * MINUTE)
    assert reply == {"stop": True}
    assert (await browser_plan(db, entries[1].start - 2 * MINUTE, settings))["created"] == 1


async def test_a_browser_run_past_its_deadline_is_abandoned(database, db, tmp_path):
    settings, entries, nodes, path = setup(database, tmp_path, run_deadline_minutes=120)
    await set_setting(db, "browser.settings_file", str(path))
    node, batch = await started(db, database, settings, nodes, entries[0])
    t = entries[0].start
    for minute in (100, 119):  # progress, yet slow: only the deadline ends it
        await nodework.progress(db, node, batch, {"requests": minute}, t + minute * MINUTE)
    assert await nodework.reap(db, t + 119 * MINUTE) == {"failed": {}, "closed": 0}
    assert await nodework.reap(db, t + 121 * MINUTE) == {"failed": {"deadline": 1}, "closed": 1}
    assert run(database, batch)[:2] == ("failed", "deadline")
    assert query(database, ALERTS, (ABANDONED_ALERT,))[0][1].endswith(
        "(ai_chatgpt) passed its deadline; abandoned"
    )
