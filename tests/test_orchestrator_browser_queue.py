"""The browser planner in queue mode (orchestrator/browser.py, K-BP5): one batch per queue entry
two minutes before its start, a missed entry caught up or skipped, slots left unchanged."""

from datetime import timedelta

import pytest
from browser_queue_cases import DAY, networks, queue_settings
from orchestrator_cases import prepare, query

from proxy_index.orchestrator.browser import MISSED_ALERT, browser_plan
from proxy_index.orchestrator.browser_queue import EPOCH, queues
from proxy_index.orchestrator.db import Database
from proxy_index.orchestrator.nodes import issue_token
from proxy_index.storage import connect

pytest_plugins = ["test_storage"]

PERIOD = timedelta(days=1)
LEAD = timedelta(seconds=60)
D = (DAY - EPOCH) // PERIOD
ROWS = (
    "SELECT b.target_id,b.scheduled_for,b.start_at,b.status,n.provider||'/'||n.product "
    "FROM test_batches b LEFT JOIN batch_runs r USING(batch_id) LEFT JOIN nodes n USING(node_id) "
    "WHERE b.kind='browser' ORDER BY b.scheduled_for"
)


@pytest.fixture
async def db(database):
    pool = Database(database)
    yield pool
    await pool.close()


def setup(database, tmp_path):
    """browser-1's 6 networks with their browser nodes; browser-2 and -3 have none."""
    prepare(database)
    for network in networks(1):
        provider, product = network.split("/")
        issue_token(
            database, provider=provider, product=product, region="browser", host="browser-1"
        )
    settings = queue_settings(tmp_path)
    return settings, queues(settings)


def at(queue, k, day=D):
    """Position k of a host's queue on `day` (k past the day's last runs into the next)."""
    return queue.position(EPOCH + PERIOD * day, LEAD, k)


async def test_each_entry_opens_two_minutes_before_its_start_once(database, db, tmp_path):
    settings, (first, second, _) = setup(database, tmp_path)
    entry = first.entries(D, PERIOD, LEAD)[0]
    assert entry.start == DAY + LEAD and entry.target == "ai_chatgpt"
    plan = {"opened": [], "created": 0, "caught_up": 0, "missed": 0, "networks": 6}
    early = await browser_plan(db, entry.start - timedelta(minutes=2, seconds=1), settings)
    assert early == plan
    # The day before has no batches, yet nothing is missed: the queue was not running.
    due = await browser_plan(db, entry.start - timedelta(minutes=2), settings)
    assert due == plan | {"opened": ["ai_chatgpt"], "created": 1}
    assert await browser_plan(db, entry.start, settings) == plan
    rows = query(database, ROWS)
    assert rows == [("ai_chatgpt", entry.start, entry.start, "scheduled", entry.network)]
    # browser-2's first run, 10 minutes later: no node for its network, closed at once.
    other = second.entries(D, PERIOD, LEAD)[0]
    assert other.start == DAY + LEAD + timedelta(minutes=10)
    opened = await browser_plan(db, other.start - timedelta(seconds=90), settings)
    assert opened["created"] == 1
    assert query(database, ROWS)[1] == ("ai_chatgpt", other.start, other.start, "failed", None)


async def test_a_restart_catches_missed_entries_up_at_free_positions(database, db, tmp_path):
    settings, (queue, *_) = setup(database, tmp_path)
    entries = queue.entries(D, PERIOD, LEAD)
    await browser_plan(db, entries[0].start - timedelta(minutes=2), settings)
    # Down from then until 3:05: the runs at 0:31 to 3:01 (positions 1 to 6) were missed.
    now = DAY + timedelta(hours=3, minutes=5)
    result = await browser_plan(db, now, settings)
    assert result == {"opened": [e.target for e in entries[1:7]], "created": 6} | {
        "caught_up": 6,
        "missed": 0,
        "networks": 6,
    }
    # From 3:31 (position 7) one each: position 1's network runs position 7 itself, so it
    # takes position 8 and position 2 takes 7.
    assert entries[1].network == entries[7].network
    places = {1: 8, 2: 7, 3: 9, 4: 10, 5: 11, 6: 12}
    rows = query(database, ROWS)[1:]
    assert rows == [
        (entries[k].target, entries[k].start, at(queue, place), "scheduled", entries[k].network)
        for k, place in places.items()
    ]
    assert await browser_plan(db, now, settings) == result | {
        "opened": [],
        "created": 0,
        "caught_up": 0,
    }
    # Position 7's own run opens on time beside the catch-up at the same start.
    own = await browser_plan(db, entries[7].start - timedelta(minutes=2), settings)
    assert own["opened"] == [entries[7].target] and own["caught_up"] == 0


async def test_a_run_that_cannot_finish_before_its_next_start_is_skipped(database, db, tmp_path):
    settings, (queue, *_) = setup(database, tmp_path)
    entries = queue.entries(D, PERIOD, LEAD)
    await browser_plan(db, entries[0].start - timedelta(minutes=2), settings)
    now = DAY + timedelta(hours=21, minutes=45)
    result = await browser_plan(db, now, settings)
    # 0:31 to 21:31 were missed: each catches up only if it ends 3 hours later at the latest
    # by the same time tomorrow, so 0:31 is skipped and later ones fill 22:01 onwards.
    assert result["caught_up"] + result["missed"] == 43 and result["missed"] >= 1
    rows = {row[1]: row for row in query(database, ROWS)}
    assert rows[entries[1].start][2:] == (entries[1].start, "skipped", None)
    caught = [rows[e.start] for e in entries[1:44] if rows[e.start][3] == "scheduled"]
    assert len(caught) == result["caught_up"]
    assert len({row[2] for row in caught}) == len(caught)
    for _, scheduled, start, _, network in caught:
        assert now <= start and start + timedelta(hours=3) <= scheduled + PERIOD
        k = (start - at(queue, 0)) // queue.gap
        assert queue.network_at(D + k // 48, k % 48) != network
    alerts = query(database, "SELECT kind,dedupe_key,occurrences,data FROM alerts")
    assert [a[:3] for a in alerts] == [
        (MISSED_ALERT, f"{MISSED_ALERT}:browser-1:2026-11-08", result["missed"])
    ]
    assert set(alerts[0][3]) == {"host", "day", "target", "country"}
    assert (alerts[0][3]["host"], alerts[0][3]["day"]) == ("browser-1", "2026-11-08")
    # Decided once: the next tick neither skips nor alerts again.
    again = await browser_plan(db, now + timedelta(minutes=1), settings)
    assert (again["created"], again["caught_up"], again["missed"]) == (0, 0, 0)
    assert query(database, "SELECT occurrences FROM alerts") == [(result["missed"],)]


async def test_a_missing_target_keeps_its_place_free(database, db, tmp_path):
    settings, (queue, *_) = setup(database, tmp_path)
    with connect(database) as conn:
        conn.execute("DELETE FROM targets WHERE target_id='ai_chatgpt'")
    entries = queue.entries(D, PERIOD, LEAD)
    first = await browser_plan(db, entries[0].start - timedelta(minutes=2), settings)
    assert first["missing"] == ["ai_chatgpt"] and first["created"] == 0
    second = await browser_plan(db, entries[1].start - timedelta(minutes=2), settings)
    assert second["opened"] == ["google_search"]
