"""BROWSER-WHOLE-RUN-1 (#1266, the owner's report of 2026-11-16): a browser run counts whole, by
its finish time, in the window that holds its end (24h, 7d, 30d, a day), all its tries or none,
for every browser target and group; a run ending after the window's end is out, one that
started before the build's raw rows is in no window. The methodology's
browser_window_rule = "whole_run"; without it a window holds the rows whose bucket starts in it."""

import copy
from datetime import UTC, datetime, timedelta

import pytest

from atlas.calculated import browser
from atlas.calculated import target_scores as ts
from atlas.calculated.aggregate import metrics, target_timed
from atlas.calculated.inputs import BucketRow, RunFields
from atlas.calculated.policy import BROWSER_WINDOW_RULES, browser_whole_runs, load, validate
from atlas.calculated.value_all import target_costs

CONFIG, METHOD_ID = load("configs/methodology.toml")
BUCKETS = copy.deepcopy(CONFIG)
del BUCKETS["browser_window_rule"]
# The owner's case: the 24h window of the 16 Nov 15:10 build starts at 15 Nov 15:10, inside a
# run from 14:49 to 15:34 of 100 tries.
END = datetime(2026, 11, 16, 15, 10, tzinfo=UTC)
SINCE = END - timedelta(hours=24)
RUN_START = datetime(2026, 11, 15, 14, 49, tzinfo=UTC)
RUN_END = datetime(2026, 11, 15, 15, 34, tzinfo=UTC)
FIVE = timedelta(minutes=5)
NETWORKS = {"a": 0.9, "b": 0.8, "c": 0.7, "d": 0.6}


def run(start, finish, tries=100, ok=0.9, target="amazon_pdp", key=1):
    """A browser run's BucketRows as build() makes them: `tries` tries spread over its 5-minute
    buckets from start to finish, `ok` of them successes, 1 kB up and 9 kB down each."""
    fields = {"target": target, "test": "browser", "engine": "chrome", "light": True}
    owner = RunFields(fields, None, 1800, finish)
    first = start.replace(minute=start.minute - start.minute % 5)
    starts = []
    while first < finish:
        starts.append(first)
        first += FIVE
    rows = []
    for i, at in enumerate(starts):
        n = tries // len(starts) + (i < tries % len(starts))
        good = round(n * ok)
        counters = {"completed": n, "successes": good, "errors": {}, "response_body_bytes": 0}
        counters |= {"latency_histogram": {"bins": [(900, good)] if good else []}}
        counters |= {"challenges": n - good, "blocks": 0}
        counters |= {"bytes_up": n * 1e3, "bytes_down": n * 9e3}
        bucket = {"bucket_start": at, "run_key": key, "counters": counters, "outcome": True}
        rows.append(BucketRow(bucket, owner))
    return rows


def window(config, rows, since=SINCE, end=END):
    """browser.targets()' browser targets of a window: {target: its rows}."""
    found = browser.targets(config, [], rows, since, end)
    return {t: own for t, own, extra in found if extra}


def tries(rows):
    return sum(r["counters"]["completed"] for r in rows)


def test_the_methodology_counts_whole_runs():
    assert CONFIG["browser_window_rule"] == "whole_run" and browser_whole_runs(CONFIG)
    assert not browser_whole_runs(BUCKETS) and BROWSER_WINDOW_RULES == ("bucket", "whole_run")
    validate(copy.deepcopy(BUCKETS) | {"browser_window_rule": "bucket"})
    for bad in ("whole", 1, None):
        with pytest.raises(ValueError, match="Invalid browser_window_rule"):
            validate(copy.deepcopy(CONFIG) | {"browser_window_rule": bad})


def test_a_bucket_row_names_its_runs_finish():
    rows = run(RUN_START, RUN_END)
    assert {r["run_finished_at"] for r in rows} == {RUN_END}
    assert "run_finished_at" not in list(rows[0])  # read by name only, never in a copy


def test_a_run_straddling_the_window_start_counts_whole_in_the_window_with_its_end():
    rows = run(RUN_START, RUN_END)
    assert rows[0]["bucket_start"] < SINCE <= RUN_END < END
    assert tries(window(CONFIG, rows)["amazon_pdp"]) == 100
    # The window before it does not hold it: once in the window with its end, never twice.
    assert window(CONFIG, rows, SINCE - timedelta(hours=24), SINCE) == {}
    # Before the rule the window held only the tries after its start (the owner's 44 to 76).
    cut = tries(window(BUCKETS, rows)["amazon_pdp"])
    assert 0 < cut < 100
    assert cut == tries([r for r in rows if r["bucket_start"] >= SINCE])


def test_a_run_ending_after_the_window_end_is_out():
    rows = run(END - timedelta(minutes=20), END + timedelta(minutes=25))
    assert window(CONFIG, rows) == {}
    assert tries(window(CONFIG, rows, END, END + timedelta(hours=24))["amazon_pdp"]) == 100
    assert 0 < tries(window(BUCKETS, rows)["amazon_pdp"]) < 100  # was cut at the end


def test_a_group_counts_its_sources_whole_runs():
    rows = run(RUN_START, RUN_END, target="instagram_web") + run(
        SINCE + timedelta(hours=1), SINCE + timedelta(hours=2), target="x_web", key=2
    )
    found = window(CONFIG, rows)
    assert tries(found["social_web"]) == 200
    assert (tries(found["instagram_web"]), tries(found["x_web"])) == (100, 100)


def test_a_run_inside_the_window_is_unchanged():
    rows = run(SINCE + timedelta(hours=3), SINCE + timedelta(hours=4))
    assert window(CONFIG, rows) == window(BUCKETS, rows)


def network(config, rows, since=SINCE, end=END):
    """A network's 24h window from its browser rows, as score_networks() reads it."""
    targets = {}
    for target, own, extra in browser.targets(config, [], rows, since, end):
        if extra:
            targets[target] = {"metrics": metrics(own, 24)} | extra
    return {"24h": {"targets": targets}}


def scored(config):
    """Each network's amazon_pdp score status: a to d, each with one 100-try run from 14:49 to
    15:34 the day before, across the 24h window's start."""
    out = []
    for i, (name, ok) in enumerate(NETWORKS.items()):
        rows = run(RUN_START, RUN_END, ok=ok, key=i)
        out.append({"id": name, "kind": "residential", "windows": network(config, rows)})
    ts.score_networks(out, config)
    return {
        n["id"]: n["windows"]["24h"]["targets"]["amazon_pdp"]["browser"]["scores"]["success"]
        for n in out
    }


def test_the_minimum_counts_a_whole_run():
    found = scored(CONFIG)
    assert {n: s["status"] for n, s in found.items()} == dict.fromkeys(NETWORKS, ts.RANKED)
    assert found["a"]["score"] == 10 and found["a"]["ranked_of"] == 4
    # Cut at the window start, each run fell under the 100 tries and did not score.
    cut = scored(BUCKETS)
    assert {s["status"] for s in cut.values()} == {ts.NOT_ENOUGH_DATA}


def test_a_short_run_still_does_not_score_whole():
    rows = run(RUN_START, RUN_END, tries=99)
    entry = network(CONFIG, rows)["24h"]["targets"]["amazon_pdp"]
    assert entry["browser"]["engines"]["chrome"]["completed"] == 99 < CONFIG["browser_min_requests"]


def test_the_value_bill_holds_whole_runs_and_is_unchanged_inside_the_window():
    """The bill's 7-day browser window (#1221) keeps its length: a run inside it costs the same,
    one across its start now costs its whole 100 tries."""
    since = END - timedelta(hours=168)
    inside = run(since + timedelta(days=2), since + timedelta(days=2, minutes=40))
    across = run(since - timedelta(minutes=20), since + timedelta(minutes=25), ok=1.0, key=2)

    def bill(config, rows):
        own = window(config, rows, since)["amazon_pdp"]
        return target_costs(2.0, {"amazon_pdp": {"metrics": metrics(own, 168)}}, config)

    assert bill(CONFIG, inside) == bill(BUCKETS, inside)
    whole = window(CONFIG, across, since)["amazon_pdp"]
    assert (tries(whole), metrics(whole, 168)["successes"]) == (100, 100)
    # 100 tries over 100 successes, 1 MB of wire traffic: 10 kB per try (VALUE-FAIL-1).
    assert bill(CONFIG, across)["amazon_pdp"] == [1.0, None, pytest.approx(1e6 / 100)]
    assert bill(BUCKETS, across)["amazon_pdp"] == [1.0, None, pytest.approx(1e6 / 100)]


def test_a_browser_targets_first_slot_is_not_cut_at_the_window_start():
    rows = run(RUN_START, RUN_END)
    extra = {"browser": {}}
    assert browser.timed_since(CONFIG, extra, SINCE) is None
    assert browser.timed_since(CONFIG, {}, SINCE) == SINCE  # an HTTP target keeps its cut
    assert browser.timed_since(BUCKETS, extra, SINCE) == SINCE
    timing = (30, 30, "generic_http")
    tm = target_timed(("amazon_pdp", rows, extra), 24, [], timing, None)
    assert tm["time_slots"]["cut_at_start"] == 0 and tm["completed"] == 100
    cut = target_timed(("amazon_pdp", rows, extra), 24, [], timing, SINCE)
    assert cut["time_slots"]["cut_at_start"] == 2  # 14:30 and 15:00 began before 15:10


def test_a_browser_run_started_before_the_raw_rows_is_in_no_window():
    start = END - timedelta(days=30)
    runs = [
        {"run_key": 1, "started_at": start - FIVE, "test": "browser", "scope": {}},
        {"run_key": 2, "started_at": start - FIVE, "test": None, "scope": scope("generic_http")},
        {"run_key": 3, "started_at": start, "test": "browser", "scope": {}},
        {"run_key": 4, "started_at": start - FIVE, "test": "pulse", "scope": scope("amazon_pdp")},
    ]
    buckets = [{"run_key": k, "bucket_start": start} for k in (1, 2, 3, 4)]
    kept = browser.horizon(CONFIG, runs, buckets, start)
    assert [b["run_key"] for b in kept] == [2, 3]
    assert browser.horizon(BUCKETS, runs, buckets, start) is buckets
    assert browser.horizon(CONFIG, runs[1:3], buckets, start) is buckets


def scope(target):
    return {"target": {"id": target}}


def test_a_day_holds_the_runs_that_end_in_it():
    rows = run(SINCE - timedelta(minutes=20), SINCE + timedelta(minutes=25))
    assert {browser.counted_at(CONFIG, r) for r in rows} == {SINCE + timedelta(minutes=25)}
    assert {browser.counted_at(BUCKETS, r) for r in rows} == {r["bucket_start"] for r in rows}
    hand_made = {"bucket_start": SINCE}
    assert browser.counted_at(CONFIG, hand_made) == SINCE
