"""BROWSER-CYCLE-22H-1: the production queue revisits every (network, target) exactly every
22 hours, so every 24-hour window holds a complete run, and at schedule.queue_start the daily
queue hands over with no pair run twice and none skipped."""

from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from test_browser_settings_production import ENGINES, TEMPLATE

from atlas.browser.contracts import RUN_TARGETS, TARGET_GROUPS, slot_stamp
from atlas.browser.settings import load_settings, queue_load
from atlas.orchestrator.browser_queue import DAY, in_force, load, near, queues

START = datetime(2026, 11, 18, tzinfo=UTC)  # the template's queue_start
PERIOD = timedelta(minutes=1320)
HOUR = timedelta(hours=1)
LONGEST_RUN = timedelta(minutes=72)  # ai_chatgpt's run, about 1.2 hours
LEAD = timedelta(seconds=60)


def daily(settings):
    """The same settings as today's daily queue: period 1440, no queue_start."""
    schedule = replace(settings.schedule, queue_period_minutes=1440, queue_start=None)
    return replace(settings, schedule=schedule)


def planned(settings, first, last):
    """The entries starting from `first` to before `last`, as the planner sees them hour by
    hour (browser_queue.near), in start order."""
    found, now = set(), first
    while now <= last:
        found.update(e for e in near(settings, now) if first <= e.start < last)
        now += HOUR
    return sorted(found, key=lambda entry: entry.start)


def starts(entries):
    """(network, target, country) -> its starts, in order."""
    found = defaultdict(list)
    for entry in entries:
        found[entry.network, entry.target, entry.country].append(entry.start)
    return found


def test_every_pair_is_revisited_exactly_every_22_hours():
    settings = load_settings(TEMPLATE)
    assert (settings.schedule.queue_period_minutes, settings.schedule.queue_start) == (1320, START)
    pairs = starts(planned(settings, START, START + PERIOD * 30))
    assert len(pairs) == 18 * 9
    for runs in pairs.values():
        assert len(runs) == 30
        assert {b - a for a, b in pairwise(runs)} == {PERIOD}
    # The time of day moves 2 hours earlier each day (22 h), over 11 days.
    first = pairs["decodo/residential", "ai_chatgpt", "US"][:13]
    assert len({run.time() for run in first[:12]}) == 12 and first[12] - first[0] == DAY * 11


def test_each_mini_runs_54_runs_24_or_25_minutes_apart_at_about_83_percent():
    settings = load_settings(TEMPLATE)
    gap = PERIOD / 54
    for h, queue in enumerate(queues(settings)):
        assert (queue.gap, queue.offset, queue.step, queue.origin) == (gap, gap * h / 3, 5, START)
        periods = [queue.entries(d, LEAD) for d in range(3)]
        for entries in periods:
            assert len(entries) == 54 and all(slot_stamp(e.start) for e in entries)
            assert sorted((e.network, e.target) for e in entries) == sorted(
                (network, target) for network in queue.networks for target in ENGINES
            )
        runs = [entry for entries in periods for entry in entries]
        assert {b.start - a.start for a, b in pairwise(runs)} == {
            timedelta(minutes=m) for m in (24, 25)
        }
        assert all(a.network != b.network for a, b in pairwise(runs))
        assert periods[0][0].start == START + LEAD + timedelta(minutes=(0, 8, 16)[h])
        assert round(load(settings, queue.host), 3) == 0.832
    assert queue_load(settings.host("browser-1")) == 0.832


def test_the_switch_runs_no_pair_twice_and_skips_none():
    settings = load_settings(TEMPLATE)
    entries = planned(settings, START - DAY * 3, START + DAY * 3)
    before = [entry for entry in entries if entry.start < START]
    # Before queue_start: today's daily queue, unchanged, so the file may be installed early.
    assert set(before) == set(planned(daily(settings), START - DAY * 3, START))
    assert {q.period for q in in_force(settings, START - LEAD).values()} == {DAY}
    assert {q.period for q in in_force(settings, START).values()} == {PERIOD}
    for (network, target, _), runs in starts(entries).items():
        last = max(run for run in runs if run < START)
        after = [run for run in runs if run >= START]
        # Its first run is in the round of its last daily run: 22 to 24 hours later.
        assert timedelta(hours=22) <= after[0] - last <= timedelta(hours=24), (network, target)
        assert {b - a for a, b in pairwise(after)} == {PERIOD}
    for host in in_force(settings, START):
        sequence = [entry for entry in entries if entry.host == host]
        assert all(a.network != b.network for a, b in pairwise(sequence))
    # The daily queue it replaces: each pair 4 hours later each day (7/6 of a day), and once
    # every 6 days 4 hours after its last run.
    old = starts(planned(daily(settings), START, START + DAY * 12))
    assert {b - a for runs in old.values() for a, b in pairwise(runs)} == {
        timedelta(hours=28),
        timedelta(hours=4),
    }


def test_every_24_hour_window_holds_a_complete_run_of_every_card_source():
    settings = load_settings(TEMPLATE)
    assert set(ENGINES) == set(RUN_TARGETS)
    assert all(set(sources) <= set(RUN_TARGETS) for sources in TARGET_GROUPS.values())

    def complete(pairs, window):
        return all(
            any(window <= run and run + LONGEST_RUN <= window + DAY for run in runs)
            for runs in pairs.values()
        )

    # Windows from HH:10 (the API's 24h window), every hour for 11 days from queue_start.
    windows = [START + timedelta(minutes=10) + HOUR * h for h in range(24 * 11)]
    pairs = starts(planned(settings, START, START + DAY * 13))
    assert len(pairs) == 18 * 9 and all(complete(pairs, window) for window in windows)
    old = starts(planned(daily(settings), START, START + DAY * 13))
    assert not all(complete(old, window) for window in windows)


def test_a_daily_period_with_queue_start_keeps_every_pair_at_its_hour():
    settings = load_settings(TEMPLATE)
    schedule = replace(settings.schedule, queue_period_minutes=1440)
    fixed = replace(settings, schedule=schedule)
    for runs in starts(planned(fixed, START, START + DAY * 8)).values():
        assert {b - a for a, b in pairwise(runs)} == {DAY}
