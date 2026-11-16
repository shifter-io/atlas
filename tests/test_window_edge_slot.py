"""WINDOW-EDGE-SLOT-1 (#1211): the time slot cut by a window's start does not vote on the
time-weighted success rate; its requests still count (calculated/aggregate.py timed())."""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from scoring_cases import T, row

from atlas.calculated.aggregate import census_split, timed

HOUR = timedelta(hours=1)
BYTEFUL = Path(__file__).parent / "fixtures" / "byteful_24h_20261116T0610.json"


def slot_row(slot, n, successes, since=None):
    """One pulse slot's requests as one bucket row: a slot cut by the window start has only its
    buckets from the start on (bucket_start), and keeps its batch's slot."""
    r = row(slot, 100, n=n, successes=successes)
    return r | {"bucket_start": max(slot, since) if since else slot}


def test_the_slot_cut_by_the_window_start_does_not_vote():
    # 24 full hours at 98%, and the hour before them cut by the window start at :10, holding
    # only the last 17 requests of one pulse, all failed (Byteful's 06:06 build).
    since = T + timedelta(minutes=10)
    first = T + HOUR
    rows = [slot_row(first + k * HOUR, 10_000, 9_800) for k in range(24)]
    rows.append(slot_row(T, 17, 0, since))
    m = timed(rows, 24, 30, slot_minutes=60, since=since)
    assert m["success_rate"] == pytest.approx(0.98)
    assert m["completed"] == 240_017 and m["successes"] == 235_200
    assert m["request_success_rate"] == pytest.approx(235_200 / 240_017)
    slots = m["time_slots"]
    assert slots["success"] == 24 and slots["cut_at_start"] == 1 and slots["down"] == 0
    # Without the window start (the rule before WINDOW-EDGE-SLOT-1) the 17 requests voted 0
    # with the weight of a full hour.
    before = timed(rows, 24, 30, slot_minutes=60)
    assert before["success_rate"] == pytest.approx(0.98 * 24 / 25)
    assert before["time_slots"]["cut_at_start"] == 0 and before["time_slots"]["success"] == 25
    # Its latency and its census overlap figures follow the same rule.
    assert m["latency_ms"] == before["latency_ms"]
    split = census_split(rows, [], 30, 60, since=since)["pulses"]["clean"]
    assert split["success_rate"] == pytest.approx(0.98) and split["requests"] == 240_017


def test_a_window_starting_on_a_slot_boundary_is_unchanged():
    rows = [slot_row(T + k * HOUR, 1_000, 900 + k) for k in range(24)]
    pulses = [{"slot": T + 2 * HOUR, "requests": 100, "successes": 0, "counted": False}]
    m = timed(rows, 24, 30, pulses, 60, since=T)
    assert m == timed(rows, 24, 30, pulses, 60)
    assert m["time_slots"]["cut_at_start"] == 0 and m["time_slots"]["success"] == 24


def byteful(target):
    """Byteful's 06:06 build rows of one target (tests/fixtures), and its window start."""
    found = json.loads(BYTEFUL.read_text())
    since = datetime.fromisoformat(found["since"])
    rows = [
        slot_row(datetime.fromisoformat(slot), n, ok, since) | {"target": target}
        for slot, n, ok in found["slots"][target]
    ]
    return rows, since


def test_byteful_06_06_build_votes_24_whole_slots():
    rows, since = byteful("generic_http")
    published = timed(rows, 24, 30, slot_minutes=60)
    # The fixture is what the build read: the published 95.196% over 25 slots.
    assert published["completed"] == 1_935_573
    assert round(published["success_rate"], 5) == 0.95196
    assert published["time_slots"]["success"] == 25
    m = timed(rows, 24, 30, slot_minutes=60, since=since)
    assert m["completed"] == 1_935_573 and m["successes"] == published["successes"]
    assert m["time_slots"]["success"] == 24 and m["time_slots"]["cut_at_start"] == 1
    assert round(m["success_rate"], 5) == 0.99162
    # generic_https: its cut hour held 100 requests at 78%.
    rows, since = byteful("generic_https")
    assert round(timed(rows, 24, 30, slot_minutes=60)["success_rate"], 5) == 0.98413
    assert round(timed(rows, 24, 30, slot_minutes=60, since=since)["success_rate"], 5) == 0.99263


@pytest.mark.parametrize("hours", [168, 720])
def test_7d_and_30d_are_unchanged_when_no_slot_is_cut(hours):
    # A window starting mid-hour whose first, cut hour holds no request: nothing is left out.
    since = T + timedelta(minutes=10)
    rows = [
        slot_row(T + k * HOUR, 1_000, 0 if k % 50 == 3 else 1_000 - k % 7 * 10)
        for k in range(1, hours)
    ]
    m = timed(rows, hours, 30, slot_minutes=60, since=since)
    assert m == timed(rows, hours, 30, slot_minutes=60)
    assert m["time_slots"]["cut_at_start"] == 0 and m["time_slots"]["down"] >= 1
