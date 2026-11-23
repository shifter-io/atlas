"""PULSE-ROOM-1 (#1412): a paced pulse run of fewer than budget.MIN_RUN connections (proxy-cheap's
2, PC-BUDGET-1) gets NARROW_ROOM (6 minutes) before the HH:52 run limit, so its last start is
HH:46, not HH:48; runs of 5 connections or more keep PACED_ROOM. Production numbers: 19 Nov
12:00 to 23 Nov 17:00 UTC, proxy-cheap's pulse runs at 2 connections."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest

from atlas.campaign import pulse_late
from atlas.daily import budget
from atlas.node_pool import RUNS_AT_ONCE

HOUR = datetime(2026, 11, 20, 1, tzinfo=UTC)
STEP = budget.pulse_step(["proxy-cheap"])  # 30 s over its two hourly targets
GAP = STEP / 2
NODE_POLL = 5  # node.py's work poll, seconds
# each region's p99 run time, seconds (tor1 on only 209 runs, 6 of them over 6 minutes)
P99 = {"ams3": 261, "blr1": 291, "fra1": 189, "lon1": 210, "nyc3": 286, "sgp1": 293, "syd1": 356}
P99 |= {"tor1": 455}


def _last(connections: dict[str, int]) -> datetime:
    return HOUR + pulse_late.last_start(STEP, pulse_late.room(connections))


@pytest.mark.parametrize(
    ("connections", "room"),
    [
        ({"proxy-cheap": 4}, pulse_late.NARROW_ROOM),  # PC-BUDGET-1: 2 a run
        ({"proxy-cheap": 9}, pulse_late.NARROW_ROOM),  # 4 a run
        ({"proxy-cheap": 10}, pulse_late.PACED_ROOM),  # 5 a run, budget.MIN_RUN
        ({"proxy-cheap": 11}, pulse_late.PACED_ROOM),  # the budget of 100: 5 a run
        ({"proxy-cheap": 30, "other": 4}, pulse_late.NARROW_ROOM),  # the slowest decides
        ({}, pulse_late.PACED_ROOM),
    ],
)
def test_a_run_under_min_run_connections_gets_the_narrow_room(connections, room):
    assert pulse_late.room(connections) == room


def test_two_connection_runs_start_by_hh46_and_others_keep_hh48():
    assert pulse_late.NARROW_ROOM == timedelta(minutes=6)
    assert _last({"proxy-cheap": 4}) == HOUR + timedelta(minutes=46)
    assert _last({"proxy-cheap": 11}) == HOUR + timedelta(minutes=48)
    assert pulse_late.last_start(None, pulse_late.NARROW_ROOM) == pulse_late.LAST_START


def test_the_hours_budget_gives_proxy_cheaps_pulse_droplets_the_narrow_room():
    # 8 pulse droplets on a budget of 40 less 8: 4 each, 2 a run (budget.pulse, PC-BUDGET-1)
    each = budget.share("proxy-cheap", 8, least=RUNS_AT_ONCE)
    assert (each, each // RUNS_AT_ONCE) == (4, 2)
    assert pulse_late.room({"proxy-cheap": each}) == pulse_late.NARROW_ROOM


@pytest.mark.parametrize(
    "planned",
    ["23:47:58", "00:46:45", "01:46:52", "01:47:20", "02:47:59", "13:47:54", "15:47:43"],
)
def test_a_run_lost_to_node_removed_in_production_is_not_given_at_2_connections(planned):
    # 19-23 Nov: 36 proxy-cheap runs planned from HH:45:55 to HH:48:00 were still running at
    # HH:52 (240 to 364 s) and failed node_removed; with HH:48 the Puller gave them all.
    minute, second = (int(part) for part in planned.split(":")[1:])
    now = HOUR + timedelta(minutes=minute, seconds=second)
    puller = pulse_late.Puller({"fra1": [1, 2]}, GAP, _last({"proxy-cheap": 4}))
    assert puller.due(now, Counter()) == []
    assert (puller.not_opened, puller.pulling) == (2, set())


def test_a_run_given_at_hh46_has_room_for_most_regions_p99():
    # 83 of the 18,769 runs took over 6 minutes, 381 over 4. With the node's work poll, 6
    # minutes hold every region's p99 but syd1's (a second over) and tor1's; 4 held only two.
    puller = pulse_late.Puller({"syd1": [1]}, GAP, _last({"proxy-cheap": 4}))
    assert puller.due(HOUR + timedelta(minutes=46), Counter()) == [("syd1", 1)]
    assert _over(pulse_late.NARROW_ROOM) == ["syd1", "tor1"]
    assert sorted(set(P99) - set(_over(pulse_late.PACED_ROOM))) == ["fra1", "lon1"]


def _over(room: timedelta) -> list[str]:
    """The regions whose p99 run, started at the node's next work poll, outlasts `room`."""
    return sorted(r for r, p99 in P99.items() if timedelta(seconds=NODE_POLL + p99) > room)
