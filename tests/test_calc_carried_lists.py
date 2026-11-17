"""CALC-SCALE-4 (#1332, the owner's decision of 17 Nov): a census-pulse group's 7d and 30d lists
are rebuilt by the first build at or after each of carried_lists.REBUILD_HOURS and kept by the
builds between, which still count everything else. At a rebuild the incremental build equals the
full one; between rebuilds it equals the full one but for those lists (and the network lists
merged from them), which are the rebuild's, from its cutoff (breakdowns_as_of).
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from calculated_cases import lenient_policy
from test_calculated_hourly_reads import HOUR, floor_hour
from test_calculated_incremental import first_difference, history, without

from atlas.calculated import carried_lists, store
from atlas.calculated.aggregate import iso
from atlas.calculated.calculator import calculate
from atlas.calculated.slim import POOL_LISTS
from atlas.storage import connect

pytest_plugins = ["test_storage"]

MINUTES = timedelta(minutes=5)
KEPT = {"24h": False, "7d": True, "30d": True}


def test_the_lists_are_rebuilt_from_the_rebuild_hours_on():
    at = datetime(2026, 11, 17, 13, 5, tzinfo=UTC)
    assert carried_lists.since(at) == at.replace(minute=0)
    assert carried_lists.since(at + 5 * HOUR) == at.replace(minute=0)  # 18:05: 13:00's
    assert carried_lists.since(at.replace(hour=0)) == at.replace(day=16, hour=19, minute=0)


WINDOW_LISTS = """SELECT product_id||':'||cohort,window_hours,list_id FROM calculated_windows
WHERE build_id=%s AND target=''"""
LABELS = {24: "24h", 168: "7d", 720: "30d"}


def built(database, policy, as_of, mode):
    """The build's payload as read back, with its windows' list ids ("_lists")."""
    payload = calculate(database, policy, as_of=as_of, mode=mode, publish=mode == "incremental")
    with connect(database) as conn:
        rows = conn.execute(WINDOW_LISTS, (int(payload["snapshot_id"]),)).fetchall()
    found = without(store.read(database, payload["snapshot_id"]))
    return found | {"_lists": {(g, LABELS[h]): i for g, h, i in rows}}


def both(database, policy, as_of):
    """The full build at as_of, then the incremental one, published: both read the same
    previous (published) build."""
    full = built(database, policy, as_of, "full")
    return built(database, policy, as_of, "incremental"), full


def census_pulse(payload):
    """{(group id, window label): its list id and breakdowns_as_of} of the 7d/30d windows that
    carry a breakdowns_as_of (the census-pulse groups')."""
    return {
        (p["id"], label): (payload["_lists"][p["id"], label], w["breakdowns_as_of"])
        for p in payload["products"]
        for label, w in p["windows"].items()
        if "breakdowns_as_of" in w
    }


def masked(payload):
    """payload without the kept lists: the census-pulse 7d/30d windows' and every network
    window's lists, their row counts and breakdowns_as_of."""
    found = json.loads(json.dumps({k: v for k, v in payload.items() if k != "_lists"}, default=str))
    drop = ("lists", "breakdown_entries", "breakdowns_as_of", "breakdowns")
    for p in found["products"]:
        for label, w in p["windows"].items():
            if KEPT[label] and "breakdowns_as_of" in w:
                for k in drop:
                    w.pop(k, None)
                w["pool"] = {k: v for k, v in w["pool"].items() if not _listed(k)}
    for n in found["networks"]:
        for w in n["windows"].values():
            for k in drop:
                w.pop(k, None)
            w["pool"] = {k: v for k, v in w["pool"].items() if not _listed(k)}
    return found


def _listed(key):
    """Whether a pool key is read from the window's lists: its pool lists and their counts."""
    return key in POOL_LISTS or key == "list_entries"


def census_pulse_figures(payload, lists):
    """{(group id, label): (completed requests, unique IPs)} of the windows in lists."""
    found = {}
    for p in payload["products"]:
        for label, w in p["windows"].items():
            if (p["id"], label) in lists:
                found[p["id"], label] = (w["metrics"]["completed"], w["pool"].get("unique_ips"))
    return found


@pytest.mark.asyncio
async def test_census_pulse_lists_are_kept_between_rebuilds(database, tmp_path):
    end = floor_hour(datetime.now(UTC)) + HOUR
    h = await history(database, tmp_path, end)
    policy = lenient_policy(tmp_path)
    second = carried_lists.since(end - HOUR) + MINUTES  # a rebuild build, at most 6 h back
    first = second - 6 * HOUR  # the rebuild before it
    # A pulse of alpha between the rebuild and the next hour's build: that build counts it.
    await h.scheduled("pulse", "alpha", end - first - 30 * timedelta(minutes=1))
    rebuilt, full = both(database, policy, first)
    assert masked(rebuilt) == masked(full), first_difference(masked(rebuilt), masked(full))
    assert rebuilt["products"] == full["products"]
    lists = census_pulse(rebuilt)
    assert lists and {at for _, at in lists.values()} == {iso(first)}
    # An hour on: the 7d and 30d census-pulse lists are the rebuild's, the rest is counted.
    kept, full = both(database, policy, first + HOUR)
    assert census_pulse(kept) == lists
    assert masked(kept) == masked(full), first_difference(masked(kept), masked(full))
    # Its own figures are counted now: the new pulse's requests are in them, not in its list.
    now, before = census_pulse_figures(kept, lists), census_pulse_figures(rebuilt, lists)
    assert now == census_pulse_figures(full, lists)
    assert any(now[k][0] > before[k][0] for k in lists), (now, before)  # alpha's pulse
    made = {
        w.get("breakdowns_as_of")
        for n in kept["networks"]
        for label, w in n["windows"].items()
        if KEPT[label] and "breakdowns_as_of" in w
    }
    assert made == {iso(first)}  # the networks with a census-pulse group say so
    # The next rebuild hour makes them again.
    again, full = both(database, policy, second)
    assert again["products"] == full["products"]
    assert {at for _, at in census_pulse(again).values()} == {iso(second)}
    assert not {i for i, _ in census_pulse(again).values()} & {i for i, _ in lists.values()}
    assert {k: v for k, v in again.items() if k != "_lists"} == {
        k: v for k, v in full.items() if k != "_lists"
    }
