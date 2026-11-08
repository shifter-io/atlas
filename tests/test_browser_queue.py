"""The browser queue (orchestrator/browser_queue.py, K-BP5): the owner's order, gaps, host
offsets and day rotation; queue settings and their refusals; the queue-mode run deadline."""

import re
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest
from browser_queue_cases import COSTS, DAY, ORDER, networks, queue_document, queue_settings
from test_browser_settings import target, write

from proxy_index.browser.run import run_deadline, run_slot, slot_deadline, slot_start
from proxy_index.browser.settings import Schedule, load_settings, queue_load
from proxy_index.errors import UserError
from proxy_index.orchestrator.browser_queue import (
    EPOCH,
    HostQueue,
    load,
    queues,
    round_order,
    step,
)

PERIOD = timedelta(days=1)
LEAD = timedelta(seconds=60)
D = (DAY - EPOCH) // PERIOD  # the owner's day, in days since the epoch


def day_entries(queue: HostQueue, day: int = D):
    return queue.entries(day, PERIOD, LEAD)


def test_the_round_alternates_heavy_and_light_targets():
    assert round_order(list(COSTS.items())) == ORDER
    # Ties keep the settings order.
    assert round_order([("a", 1), ("b", 2), ("c", 2), ("d", 1)]) == ["b", "d", "c", "a"]
    assert round_order([("only", 5)]) == ["only"] and round_order([]) == []


def test_the_owners_queue_starts_a_run_every_30_minutes(tmp_path):
    settings = queue_settings(tmp_path)
    hosts = queues(settings)
    assert [q.host for q in hosts] == ["browser-1", "browser-2", "browser-3"]
    assert {q.gap for q in hosts} == {timedelta(minutes=30)}
    assert [q.offset for q in hosts] == [timedelta(minutes=m) for m in (0, 10, 20)]
    for h, queue in enumerate(hosts):
        entries = day_entries(queue)
        starts = [entry.start for entry in entries]
        assert len(set(starts)) == 48
        assert starts[0] == DAY + LEAD + timedelta(minutes=10 * h)
        assert {b - a for a, b in pairwise(starts)} == {timedelta(minutes=30)}
        # Every network meets every target exactly once a day.
        pairs = [(entry.network, entry.target) for entry in entries]
        assert sorted(pairs) == sorted((n, t) for n in networks(h + 1) for t in COSTS)
        # Each round is the heavy-light order.
        assert [entry.target for entry in entries[:8]] == ORDER
        assert [entry.target for entry in entries] == ORDER * 6
        assert {entry.host for entry in entries} == {queue.host}
        assert {entry.country for entry in entries} == {"US"}


def test_heavy_and_light_alternate_and_no_network_runs_twice_in_a_row(tmp_path):
    queue = queues(queue_settings(tmp_path))[0]
    two_days = day_entries(queue) + day_entries(queue, D + 1)
    for a, b in pairwise(two_days):
        assert a.network != b.network
    costs = [COSTS[entry.target] for entry in two_days]
    for i, (a, b) in enumerate(pairwise(costs[:8])):
        assert (a > b) if i % 2 == 0 else (a < b)


def test_each_day_shifts_the_hour_a_network_meets_a_target(tmp_path):
    queue = queues(queue_settings(tmp_path))[0]

    def hours(day):
        start = EPOCH + PERIOD * day
        return {(e.network, e.target): e.start - start for e in day_entries(queue, day)}

    today, tomorrow = hours(D), hours(D + 1)
    assert all(today[pair] != tomorrow[pair] for pair in today)
    # The network of position p of round r is (p + s × r + d) mod 6, with s = 5 for 8 targets.
    assert queue.step == 5
    assert queue.network_at(D + 1, 0) == queue.network_at(D, 1)


@pytest.mark.parametrize("n", range(4, 10))
@pytest.mark.parametrize("positions", range(1, 13))
def test_every_step_covers_every_pair_and_never_repeats_a_network(n, positions):
    names = tuple(f"n{i}" for i in range(n))
    items = tuple((f"t{p}", "US") for p in range(positions))
    gap = timedelta(minutes=5)
    queue = HostQueue("browser-1", names, items, gap, timedelta(0), step(n, positions))
    entries = day_entries(queue) + day_entries(queue, D + 1)
    assert all(a.network != b.network for a, b in pairwise(entries))
    pairs = {(e.network, e.target) for e in entries[: n * positions]}
    assert len(pairs) == n * positions


def test_the_step_falls_back_to_one_when_no_step_avoids_a_repeat():
    assert step(6, 8) == 5 and step(6, 7) == 1 and step(5, 8) == 4 and step(3, 8) == 2
    # One or two networks, or three with a multiple of 3 positions: none avoids every repeat.
    assert step(1, 8) == 1 and step(2, 8) == 1 and step(3, 6) == 1


def test_countries_repeat_the_round_per_country(tmp_path):
    doc = queue_document()
    doc["schedule"]["countries"] = ["US", "DE"]
    doc["browser"]["browsers_per_host"] = 3
    queue = queues(queue_settings(tmp_path, doc))[0]
    assert queue.gap == timedelta(minutes=15)
    assert queue.items == tuple((t, c) for c in ("US", "DE") for t in ORDER)


def test_the_load_is_reported_by_the_settings_check(tmp_path):
    settings = queue_settings(tmp_path)
    assert load(settings, "browser-1") == pytest.approx(127_380 / 172_800)
    assert queue_load(settings.host("browser-2")) == 0.737
    assert queue_load(load_settings(write(tmp_path, slots_document()))) is None


def slots_document():
    doc = queue_document()
    del doc["schedule"]["plan"]
    return doc


def test_the_queue_fields_default_to_todays_slots(tmp_path):
    doc = slots_document()
    for entry in doc["target"]:
        del entry["browser_seconds"]
    settings = load_settings(write(tmp_path, doc))
    assert settings.schedule.plan == "slots"
    assert settings.schedule.queue_period_minutes == 1440
    assert settings.schedule.run_deadline_minutes == 180
    assert {t.browser_seconds for t in settings.targets} == {None}
    doc["schedule"].update(plan="queue", queue_period_minutes=720, run_deadline_minutes=60)
    for entry in doc["target"]:
        entry.update(every_minutes=720, browser_seconds=1)
    settings = load_settings(write(tmp_path, doc))
    assert (settings.schedule.queue_period_minutes, settings.schedule.run_deadline_minutes) == (
        720,
        60,
    )


QUEUE_CASES = [
    (
        lambda d: target(d, "google_search").pop("browser_seconds"),
        r"^target\[1\]\.browser_seconds: is required in queue mode",
    ),
    (
        lambda d: target(d, "ai_gemini").update(every_minutes=60),
        r"^target\[7\]\.every_minutes: must equal schedule\.queue_period_minutes",
    ),
    (
        lambda d: target(d, "amazon_pdp").update(browser_seconds=0),
        r"^target\[0\]\.browser_seconds: must be an integer",
    ),
    (lambda d: d["schedule"].update(plan="weekly"), r"^schedule\.plan: must be one of"),
    (lambda d: d["schedule"].update(queue_period_minutes=4), r"^schedule\.queue_period_minutes"),
    (lambda d: d["schedule"].update(run_deadline_minutes=1441), r"^schedule\.run_deadline_minutes"),
    (lambda d: d["schedule"].update(every=1), r"^schedule: unknown fields"),
    # 6 networks x 8 targets in 3 hours: a start every 3.75 minutes.
    (
        lambda d: (
            [d["schedule"].update(queue_period_minutes=180)]
            + [entry.update(every_minutes=180) for entry in d["target"]]
        ),
        r"^host\[0\]\.networks: starts would be under 5 minutes apart",
    ),
    # One browser per host: 147% of its day.
    (
        lambda d: d["browser"].update(browsers_per_host=1),
        re.escape("host[0].networks: the estimated load 147% is over 100%"),
    ),
]


@pytest.mark.parametrize("change, message", QUEUE_CASES)
def test_queue_mode_refuses_what_it_cannot_plan(tmp_path, change, message):
    doc = queue_document()
    change(doc)
    with pytest.raises(UserError, match=message):
        load_settings(write(tmp_path, doc))


def test_two_hosts_may_not_start_a_target_at_the_same_moment(tmp_path):
    # One target: 4 networks start every 6 hours, 2 networks every 12 hours from 6 hours in.
    doc = queue_document(hosts=(4, 2))
    doc["target"] = [target(doc, "ai_chatgpt")]
    with pytest.raises(UserError, match=re.escape("host[1]: starts a target when another")):
        load_settings(write(tmp_path, doc))
    doc["host"][1]["networks"] = networks(2, 4)
    assert load_settings(write(tmp_path, doc)).schedule.plan == "queue"


def test_the_queue_deadline_lets_a_late_run_finish():
    queue = Schedule(("US",), 60, plan="queue")
    slots = Schedule(("US",), 60)
    late = "2026-11-08T23:30:00+00:00"
    # Queue mode: 180 minutes from its start, minus 2: about 02:28 the next day.
    assert run_deadline(late, 1440, queue) == datetime(2026, 11, 9, 2, 28, tzinfo=UTC).timestamp()
    assert run_deadline(late, 1440, slots) == slot_deadline(late, 1440)
    assert slot_deadline(late, 1440) == datetime(2026, 11, 8, 23, 58, tzinfo=UTC).timestamp()
    # The bundle's slot: the run's own start in queue mode, else its target slot.
    assert run_slot(late, 1440, queue) == datetime(2026, 11, 8, 23, 30, tzinfo=UTC).timestamp()
    assert run_slot(late, 1440, slots) == slot_start(late, 1440) == DAY.timestamp()
