"""CHROME-STALE-EXCLUDE-1 (the owner's decision of 2026-11-22): the methodology's
[[browser_exclusions]] leave the amazon_pdp Chrome runs started from Chrome 158's Stable release
(17 Nov 19:07 UTC) until the last browser host ran an up-to-date Chrome (20 Nov 13:10 UTC) out
of every figure, for every network the same way; every other run is unchanged."""

import copy
from datetime import UTC, datetime, timedelta

import pytest

from atlas.calculated import browser
from atlas.calculated import card_success as cs
from atlas.calculated.exclusions import spans
from atlas.calculated.policy import load, validate

CONFIG = load("configs/methodology.toml")[0]
WITHOUT = {k: v for k, v in CONFIG.items() if k != "browser_exclusions"}
FROM = datetime(2026, 11, 17, 19, 7, tzinfo=UTC)
BEFORE = datetime(2026, 11, 20, 13, 10, tzinfo=UTC)
MINUTE = timedelta(minutes=1)
# The 7-day window of the build the report recounted: runs by their end.
END = datetime(2026, 11, 22, 4, 10, tzinfo=UTC)
SINCE = END - timedelta(days=7)


def at(day, hour, minute):
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


def run_row(key, target, started, engine="chrome", test="browser"):
    """A RUNS row as incremental.read() has it."""
    config = {"browser_engine": engine} if engine else {}
    scope = {"target": {"id": target}, "config": config}
    return {"run_key": key, "test": test, "scope": scope, "started_at": started}


def bucket(key, target, started, ok, engine="chrome", tries=100):
    """A run's one bucket row, counted by its end 6 minutes after its start."""
    counters = {"completed": tries, "successes": ok, "errors": {}, "response_body_bytes": 0}
    counters |= {"challenges": tries - ok, "blocks": 0, "latency_histogram": {"bins": []}}
    found = {"run_key": key, "target": target, "test": "browser", "engine": engine}
    found |= {"bucket_start": started, "run_finished_at": started + 6 * MINUTE}
    return found | {"counters": counters}


def test_the_methodology_names_the_outdated_chrome_span_of_amazon_pdp_only():
    assert spans(CONFIG) == {("amazon_pdp", "chrome"): [(FROM, BEFORE)]}
    assert spans(WITHOUT) == {}
    validate(copy.deepcopy(CONFIG))
    validate(copy.deepcopy(WITHOUT))


@pytest.mark.parametrize(
    "change",
    [
        {"target": "social_web"},  # a group is never run
        {"target": "generic_http"},
        {"engine": "firefox"},
        {"reason": ""},
        {"started_from": "2026-11-17T19:07:00"},  # no offset
        {"started_before": "yesterday"},
        {"started_before": "2026-11-17T19:07:00Z"},  # an empty span
        {"extra": 1},
    ],
)
def test_an_invalid_entry_is_refused(change):
    entry = CONFIG["browser_exclusions"][0] | change
    with pytest.raises(ValueError, match="Invalid browser_exclusions"):
        validate(copy.deepcopy(CONFIG) | {"browser_exclusions": [entry]})
    with pytest.raises(ValueError, match="Invalid browser_exclusions"):
        validate(copy.deepcopy(CONFIG) | {"browser_exclusions": CONFIG["browser_exclusions"][0]})


def test_only_amazon_pdp_chrome_runs_started_in_the_span_are_left_out():
    cases = [
        (run_row(1, "amazon_pdp", FROM), True),  # the span's first instant
        (run_row(2, "amazon_pdp", BEFORE - MINUTE), True),
        (run_row(3, "amazon_pdp", at(18, 21, 12)), True),
        (run_row(4, "amazon_pdp", FROM - MINUTE), False),  # started before, ends inside: kept
        (run_row(5, "amazon_pdp", BEFORE), False),  # an up-to-date host's
        (run_row(6, "amazon_pdp", at(18, 21, 12), engine="camoufox"), False),
        (run_row(7, "amazon_pdp", at(18, 21, 12), engine=None), False),  # Camoufox by default
        (run_row(8, "google_search", at(18, 21, 12)), False),
        (run_row(9, "linkedin_public", at(19, 3, 0)), False),
        (run_row(10, "instagram_web", at(19, 3, 0)), False),
        (run_row(11, "ai_chatgpt", at(19, 3, 0)), False),
        (run_row(12, "generic_http", at(19, 3, 0), engine=None, test="pulse"), False),
    ]
    runs = [r for r, _ in cases]
    buckets = [{"run_key": r["run_key"], "n": n} for n in range(2) for r in runs]
    kept = browser.excluded(CONFIG, runs, buckets)
    left_out = {r["run_key"] for r, out in cases if out}
    assert kept == [b for b in buckets if b["run_key"] not in left_out]
    assert {b["run_key"] for b in kept} == {r["run_key"] for r, out in cases if not out}
    # Without an entry, and with no run in a span, the rows are the very same list.
    assert browser.excluded(WITHOUT, runs, buckets) is buckets
    assert browser.excluded(CONFIG, [r for r, out in cases if not out], buckets) is buckets


# Two networks' amazon_pdp Chrome runs in the 7-day window of the 22 Nov 04:10 build, 100 tries
# each, with the successes of the investigation's recount (its section a): (start, successes).
# Network A: 67.62% published, 77.70% without the three outdated-Chrome runs. Network B: 88.00%
# and 89.60%; its seven later runs' total (628) is the report's, their split is made up.
RUNS = {
    "net_a": [
        (at(15, 10, 0), 77),
        (at(16, 10, 0), 92),
        (at(17, 23, 7), 94),  # outdated Chrome: left out
        (at(18, 21, 12), 4),  # outdated Chrome: left out
        (at(19, 19, 12), 4),  # outdated Chrome: left out
        *zip(
            [at(20, 14, 28), at(20, 18, 8), at(20, 23, 38), at(21, 5, 8)]
            + [at(21, 10, 38), at(21, 16, 8), at(21, 21, 38), at(22, 3, 8)],
            [97, 82, 65, 49, 97, 95, 51, 72],
            strict=True,
        ),
    ],
    "net_b": [
        (at(15, 12, 0), 84),
        (at(16, 12, 0), 93),
        (at(17, 12, 0), 91),
        (at(18, 12, 0), 93),  # outdated Chrome: left out
        (at(19, 8, 0), 95),  # outdated Chrome: left out
        (at(19, 23, 8), 60),  # outdated Chrome: left out
        *zip(
            [at(20, 15, 21), at(20, 19, 13), at(21, 6, 12), at(21, 17, 14)]
            + [at(21, 22, 42), at(22, 0, 12), at(22, 3, 12)],
            [84, 92, 90, 93, 85, 92, 92],
            strict=True,
        ),
    ],
}
# Each network's other browser target in the same days, started inside the span too.
OTHER = (("google_search", at(18, 9, 0), 70), ("linkedin_public", at(19, 9, 0), 55))


def network(name, config):
    """A network's rows of its runs, after browser.excluded() under config."""
    runs, buckets = [], []
    for n, (started, ok) in enumerate(RUNS[name]):
        runs.append(run_row(n, "amazon_pdp", started))
        buckets.append(bucket(n, "amazon_pdp", started, ok))
    for n, (target, started, ok) in enumerate(OTHER, start=len(runs)):
        runs.append(run_row(n, target, started))
        buckets.append(bucket(n, target, started, ok))
    return browser.excluded(config, runs, buckets)


def rate(found):
    return round(100 * found[1] / found[0], 2)


@pytest.mark.parametrize(
    ("name", "published", "recounted", "runs"),
    [("net_a", 67.62, 77.70, 10), ("net_b", 88.00, 89.60, 10)],
)
def test_the_recount_gives_the_investigations_amazon_rates(name, published, recounted, runs):
    before = cs.browser_counts(WITHOUT, network(name, WITHOUT), SINCE, END)
    after = cs.browser_counts(CONFIG, network(name, CONFIG), SINCE, END)
    assert (before["amazon_pdp"][0], rate(before["amazon_pdp"])) == (1300, published)
    assert (after["amazon_pdp"][0], rate(after["amazon_pdp"])) == (100 * runs, recounted)
    # Every other target's counts are unchanged.
    assert {t: c for t, c in after.items() if t != "amazon_pdp"} == {
        t: c for t, c in before.items() if t != "amazon_pdp"
    }
    # The Targets page cell (the window's browser object) reads the same tries as the Success
    # x/10 and the bill (card_success.counts()).
    for label, hours in (("24h", 24), ("7d", 168), ("30d", 720)):
        since = cs.browser_since(CONFIG, END, hours)
        rows = network(name, CONFIG)
        card = cs.counts(CONFIG, {}, rows, since, END)["amazon_pdp"]
        found = {t: extra for t, _, extra in browser.targets(CONFIG, [], rows, since, END)}
        cell = found["amazon_pdp"]["browser"]["engines"]["chrome"]
        assert [cell["completed"], cell["successes"]] == card, label


def test_a_day_inside_the_span_has_no_amazon_pdp_run_left():
    day = (at(18, 4, 10), at(19, 4, 10))
    for name in RUNS:
        found = cs.browser_counts(CONFIG, network(name, CONFIG), *day)
        assert "amazon_pdp" not in found
        assert found["google_search"] == [100, 70]
