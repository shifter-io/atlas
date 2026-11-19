"""The browser planner in queue mode (orchestrator/browser.py, K-BP5): one batch per queue entry
two minutes before its start, a missed entry started once a place frees, caught up or skipped,
slots left unchanged, and the switch to a 22-hour queue at queue_start (BROWSER-CYCLE-22H-1)."""

from dataclasses import replace
from datetime import timedelta

import pytest
from browser_queue_cases import DAY, networks, queue_document, queue_settings
from orchestrator_cases import prepare, query

from atlas.orchestrator.browser import GRACE, MISSED_ALERT, browser_plan
from atlas.orchestrator.browser_hosts import OPEN_AHEAD
from atlas.orchestrator.browser_queue import EPOCH, near, queues
from atlas.orchestrator.db import Database
from atlas.orchestrator.nodes import issue_token
from atlas.storage import connect

pytest_plugins = ["test_storage"]

PERIOD = timedelta(days=1)
LEAD = timedelta(seconds=60)
D = (DAY - EPOCH) // PERIOD
START = DAY + PERIOD  # a queue_start
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
    entry = first.entries(D, LEAD)[0]
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
    # browser-2's first run, 8 min 53 s later down to its minute: no node for its network,
    # closed at once.
    other = second.entries(D, LEAD)[0]
    assert other.start == DAY + LEAD + timedelta(minutes=8)
    opened = await browser_plan(db, other.start - timedelta(seconds=90), settings)
    assert opened["created"] == 1
    assert query(database, ROWS)[1] == ("ai_chatgpt", other.start, other.start, "failed", None)


def rows_now(database):
    return {row[1]: row for row in query(database, ROWS)}


def done(database):
    """Every open browser run ends: the host's places are free again."""
    query(
        database,
        "UPDATE batch_runs SET status='done',finished_at=now() "
        "WHERE status IN ('queued','running') RETURNING 1",
    )


async def test_a_restart_starts_missed_entries_as_places_free(database, db, tmp_path):
    """BROWSER-COVERAGE-1: a missed entry starts at once when its host has a free place (no
    browser idles while runs wait), the others wait and start as places free."""
    settings, (queue, *_) = setup(database, tmp_path)
    entries = queue.entries(D, LEAD)
    await browser_plan(db, entries[0].start - timedelta(minutes=2), settings)
    done(database)
    # Down from then until 3:03: the runs at 0:27 to 2:41 (positions 1 to 6) were missed.
    now = DAY + timedelta(hours=3, minutes=3)
    result = await browser_plan(db, now, settings)
    assert result == {"opened": [e.target for e in entries[1:3]], "created": 2} | {
        "caught_up": 2,
        "missed": 0,
        "waiting": 4,
        "networks": 6,
    }
    rows = query(database, ROWS)[1:]
    assert rows == [(e.target, e.start, now, "scheduled", e.network) for e in entries[1:3]]
    # Decided once; then each freed place takes the next one at once, in start order.
    again = await browser_plan(db, now, settings)
    assert (again["created"], again["waiting"]) == (0, 4)
    later = now + timedelta(minutes=1)
    done(database)
    freed = await browser_plan(db, later, settings)
    assert freed["opened"] == [e.target for e in entries[3:5]] and freed["waiting"] == 2
    assert {row[1:3] for row in query(database, ROWS)[3:]} == {
        (e.start, later) for e in entries[3:5]
    }


async def test_a_late_entry_whose_network_runs_takes_a_later_position(database, db, tmp_path):
    """A missed entry whose network has an open run on its host does not start beside it: it
    takes the host's next position whose own run is on another network, as before."""
    settings, (queue, *_) = setup(database, tmp_path)
    roomy = replace(settings, browser=replace(settings.browser, browsers_per_host=9))
    entries = queue.entries(D, LEAD)
    await browser_plan(db, entries[0].start - timedelta(minutes=2), roomy)
    # Position 0's run is still running when the planner comes back after position 6 (its
    # network's next): positions 1 to 5 start at once, position 6 takes a later position.
    six = next(k for k in range(1, 54) if entries[k].network == entries[0].network)
    query(
        database,
        "UPDATE batch_runs SET status='running',started_at=%s RETURNING 1",
        (entries[0].start,),
    )
    late = entries[six].start + timedelta(minutes=2)
    result = await browser_plan(db, late, roomy)
    assert result["caught_up"] == six and "waiting" not in result
    starts = dict((row[1], row[2]) for row in query(database, ROWS))
    assert [starts[e.start] for e in entries[1:six]] == [late] * (six - 1)
    assert starts[entries[six].start] > late  # not beside its network's open run
    k = round((starts[entries[six].start] - at(queue, 0)) / queue.gap)
    assert starts[entries[six].start] == at(queue, k)
    assert queue.network_at(D, k) != entries[0].network


async def test_a_run_that_cannot_finish_before_its_next_start_is_skipped(database, db, tmp_path):
    settings, (queue, *_) = setup(database, tmp_path)
    entries = queue.entries(D, LEAD)
    await browser_plan(db, entries[0].start - timedelta(minutes=2), settings)
    done(database)
    now = DAY + timedelta(hours=21, minutes=30)
    result = await browser_plan(db, now, settings)
    # 0:27 to 21:21 were missed: a run ends 3 hours after it starts at the latest, by the same
    # time tomorrow, so 0:27 is skipped, the next two start now and the rest wait for places.
    assert (result["missed"], result["caught_up"], result["waiting"]) == (1, 2, 45)
    rows = {row[1]: row for row in query(database, ROWS)}
    assert rows[entries[1].start][2:] == (entries[1].start, "skipped", None)
    assert [rows[e.start][2:4] for e in entries[2:4]] == [(now, "scheduled")] * 2
    alerts = query(database, "SELECT kind,dedupe_key,occurrences,data FROM alerts")
    assert [a[:3] for a in alerts] == [(MISSED_ALERT, f"{MISSED_ALERT}:browser-1:2026-11-08", 1)]
    assert set(alerts[0][3]) == {"host", "day", "target", "country"}
    assert (alerts[0][3]["host"], alerts[0][3]["day"]) == ("browser-1", "2026-11-08")
    # Decided once: the next tick neither skips nor alerts again.
    again = await browser_plan(db, now + timedelta(minutes=1), settings)
    assert (again["created"], again["caught_up"], again["missed"]) == (0, 0, 0)
    assert query(database, "SELECT occurrences FROM alerts") == [(1,)]
    # A waiting entry that can no longer finish is skipped even while its host is full: at
    # 22:50 a run of the 1:47 entry would end after 1:47 tomorrow.
    query(database, "UPDATE batch_runs SET status='running' WHERE status='queued' RETURNING 1")
    full = await browser_plan(db, DAY + timedelta(hours=22, minutes=50), settings)
    assert full["missed"] == 1 and full["waiting"] >= 1  # created: other hosts' on-time entries
    assert rows_now(database)[entries[4].start][3] == "skipped"


async def test_a_missing_target_keeps_its_place_free(database, db, tmp_path):
    settings, (queue, *_) = setup(database, tmp_path)
    with connect(database) as conn:
        conn.execute("DELETE FROM targets WHERE target_id='ai_chatgpt'")
    entries = queue.entries(D, LEAD)
    first = await browser_plan(db, entries[0].start - timedelta(minutes=2), settings)
    assert first["missing"] == ["ai_chatgpt"] and first["created"] == 0
    second = await browser_plan(db, entries[1].start - timedelta(minutes=2), settings)
    assert second["opened"] == ["x_web"]


def switched(tmp_path):
    """The owner's queue every 22 hours from START, the daily queue before it."""
    doc = queue_document()
    doc["schedule"].update(queue_period_minutes=1320, queue_start=START)
    for entry in doc["target"]:
        entry["every_minutes"] = 1320
    return queue_settings(tmp_path, doc)


def batches(database):
    return {(row[0], row[1], row[2], row[4]) for row in query(database, ROWS)}


def expected(settings, first, last):
    """(target, scheduled_for, start_at, network) of the entries from first to last, each on time;
    only browser-1's networks have nodes."""
    return {
        (e.target, e.start, e.start, e.network if e.host == "browser-1" else None)
        for e in near(settings, last)
        if first <= e.start <= last
    }


async def test_the_switch_opens_every_entry_once_on_time(database, db, tmp_path):
    setup(database, tmp_path)
    settings = switched(tmp_path)
    first, last = START - timedelta(minutes=40), START + timedelta(minutes=40)
    now = first
    while now <= last:
        result = await browser_plan(db, now, settings)
        assert (result["caught_up"], result["missed"], "waiting" in result) == (0, 0, False)
        now += timedelta(minutes=1)
    want = expected(settings, first - GRACE, last + OPEN_AHEAD)
    assert batches(database) == want
    # The daily queue's last runs, then the 22-hour queue's first ones.
    assert min(row[1] for row in want) < START <= max(row[1] for row in want)


async def test_a_planner_down_across_the_switch_catches_both_queues_up(database, db, tmp_path):
    setup(database, tmp_path)
    settings = switched(tmp_path)
    # On time from 23:00 to 23:30, so every host has begun; then down until 00:50.
    first, down = START - timedelta(hours=1), START - timedelta(minutes=30)
    tick = first
    while tick <= down:
        await browser_plan(db, tick, settings)
        tick += timedelta(minutes=1)
    now = START + timedelta(minutes=50)
    second = timedelta(seconds=1)
    late = expected(settings, down + OPEN_AHEAD + second, now - GRACE - second)
    assert any(row[1] < START for row in late) and any(row[1] >= START for row in late)
    # Each tick the free places take the missed entries at once, then the runs end.
    caught = 0
    for minute in range(len(late) + 1):
        result = await browser_plan(db, now + timedelta(minutes=minute), settings)
        assert result["missed"] == 0
        caught += result["caught_up"]
        done(database)
        if "waiting" not in result:
            break
    assert caught >= len(late)  # and the entries that fell due during those ticks
    rows = {row[:2] for row in batches(database)}
    assert {row[:2] for row in expected(settings, first - GRACE, now + OPEN_AHEAD)} <= rows


PAUSED_ROWS = (
    "SELECT b.scheduled_for,b.start_at,b.status,b.paused_network,count(r.batch_id) "
    "FROM test_batches b LEFT JOIN batch_runs r USING(batch_id) WHERE b.kind='browser' "
    "AND b.scheduled_for=ANY(%s) GROUP BY b.batch_id ORDER BY b.scheduled_for"
)


async def test_a_paused_networks_entry_is_skipped_and_marked_with_it(database, db, tmp_path):
    """VERIFY-PAUSED-BATCH-1: an entry of a paused network ("<network>/paused", netpause) is a
    'skipped' batch at its own start with paused_network set and no run, on time or missed (no
    catch-up), decided once. An entry with no node of its network still fails, unmarked."""
    setup(database, tmp_path)
    doc = queue_document()
    doc["host"][0]["networks"][0] = "example-10/paused"  # its node is example-10/residential
    settings = queue_settings(tmp_path, doc)
    first, second, _ = queues(settings)
    other = second.entries(D, LEAD)[0]  # browser-2 has no node: failed, not paused
    await browser_plan(db, other.start - timedelta(seconds=90), settings)
    assert query(database, PAUSED_ROWS, ([other.start],)) == [
        (other.start, other.start, "failed", None, 0)
    ]
    paused = [e for e in first.entries(D, LEAD) if e.network == "example-10/paused"]
    now = paused[0].start - timedelta(minutes=2)
    result = await browser_plan(db, now, settings)
    assert (result["paused"], result["created"]) == (1, 0)
    assert "paused" not in await browser_plan(db, now, settings)
    # Down from then until an hour after its next entry: that one is not caught up.
    later = paused[1].start + timedelta(hours=1)
    assert (await browser_plan(db, later, settings))["paused"] == 1
    starts = [paused[0].start, paused[1].start]
    assert query(database, PAUSED_ROWS, (starts,)) == [
        (start, start, "skipped", "example-10", 0) for start in starts
    ]
