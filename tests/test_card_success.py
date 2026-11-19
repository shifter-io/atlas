"""SUCCESS-ALL-8-1: the tries the Success x/10 counts per card target (calculated/card_success.py),
the all-targets bill's: an HTTP target's every request, a browser target's lead-engine runs by
their end (a group: its sources' union), a 24h window's browser targets over the 7 days."""

import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

from atlas.calculated import card_success as cs
from atlas.calculated.aggregate import in_window
from atlas.calculated.policy import load

CONFIG = load(Path("configs/methodology.toml"))[0]
END = datetime(2026, 11, 19, 5, 10, tzinfo=UTC)
HOUR = timedelta(hours=1)


def row(target, completed, successes, at=END - HOUR, test=None, **extra):
    counters = {"completed": completed, "successes": successes}
    return {"target": target, "test": test, "bucket_start": at, "counters": counters} | extra


def run(target, completed, successes, ended, engine=None):
    """A browser run's row: it counts by its end (whole runs), whatever its bucket's start."""
    found = row(target, completed, successes, ended - HOUR, "browser", run_finished_at=ended)
    return found | ({"engine": engine} if engine else {})


def test_an_http_target_counts_every_request():
    rows = [row("generic_http", 600, 590), row("generic_http", 400, 390, test="pulse")]
    rows += [row("generic_http", 10, 0, test="census"), row("generic_http", 10, 5, test="load")]
    rows += [row("generic_https", 100, 99), row("amazon_pdp", 100, 50)]
    found = cs.http_counts(CONFIG, rows)
    assert found == {"generic_http": [1020, 985], "generic_https": [100, 99]}


def test_a_browser_target_counts_its_lead_engines_runs_ending_in_the_window():
    since = END - timedelta(days=7)
    rows = [
        run("amazon_pdp", 100, 60, END - 2 * HOUR),
        run("amazon_pdp", 100, 80, END - 3 * 24 * HOUR),
        run("amazon_pdp", 100, 10, since - HOUR),  # ended before the window
        run("amazon_pdp", 100, 0, END + HOUR),  # ends after it
        run("google_search", 100, 50, END - HOUR, "chrome"),
        run("google_search", 200, 40, END - HOUR),  # Camoufox: the most tries
    ]
    found = cs.browser_counts(CONFIG, rows, since, END)
    assert found == {"amazon_pdp": [200, 140], "google_search": [200, 40]}


def test_a_group_is_the_union_of_its_sources_never_a_source_alone():
    rows = [
        run("instagram_web", 100, 90, END - HOUR, "chrome"),
        run("x_web", 100, 70, END - HOUR, "chrome"),
        run("x_web", 300, 300, END - HOUR),  # Camoufox X: social_web is Chrome's (VALUE-FAIL-1)
        run("ai_overview", 500, 400, END - HOUR),
        run("ai_chatgpt", 400, 200, END - HOUR),
        run("ai_gemini", 300, 300, END - HOUR, "chrome"),  # the engine with fewer tries
    ]
    found = cs.browser_counts(CONFIG, rows, END - 24 * HOUR, END)
    assert found == {"social_web": [200, 160], "ai_answers": [900, 600]}


def test_the_24h_window_reads_its_browser_targets_over_the_7_days():
    assert cs.browser_since(CONFIG, END, 24) == END - timedelta(days=7)
    assert cs.browser_since(CONFIG, END, 168) == END - timedelta(days=7)
    assert cs.browser_since(CONFIG, END, 720) == END - timedelta(days=30)
    assert cs.browser_since({}, END, 24) == END - 24 * HOUR  # without a browser_window
    old = [run("linkedin_public", 100, 14, END - 3 * 24 * HOUR)]
    http = {"generic_http": [1000, 980]}
    found = cs.counts(CONFIG, http, old, cs.browser_since(CONFIG, END, 24), END)
    assert found == {"generic_http": [1000, 980], "linkedin_public": [100, 14]}


def test_counts_keep_card_order_and_drop_targets_without_a_completed_try():
    rows = [run("linkedin_public", 300, 0, END - HOUR), run("amazon_pdp", 0, 0, END - HOUR)]
    http = {"generic_https": [10, 9], "generic_http": [20, 19]}
    found = cs.counts(CONFIG, http, rows, END - 24 * HOUR, END)
    assert list(found) == ["generic_http", "generic_https", "linkedin_public"]
    assert found["linkedin_public"] == [300, 0]  # tries and no success: counted, at 0%


def test_trailing_counts_are_the_in_window_rows_counts():
    """Prefix sums give exactly http_counts() of in_window() for any span, the 24h window's with
    the daily tests it reaches back to."""
    rng = random.Random(1384)
    start = END - timedelta(days=10)
    rows = []
    for _ in range(400):
        at = start + timedelta(minutes=rng.randrange(0, 10 * 24 * 60, 5))
        target = rng.choice(("generic_http", "generic_https", "amazon_pdp"))
        completed = rng.randrange(0, 50)
        successes, reach = rng.randrange(0, completed + 1), rng.random() < 0.1
        rows.append(row(target, completed, successes, at, reach=reach))
    trailing = cs.Trailing(CONFIG, rows)
    assert trailing.first == min(r["bucket_start"] for r in rows if r["target"] != "amazon_pdp")
    for _ in range(50):
        stop = start + timedelta(minutes=rng.randrange(0, 10 * 24 * 60))
        for hours in (24, 168):
            since = stop - timedelta(hours=hours)
            want = cs.http_counts(CONFIG, in_window(rows, since, stop, hours))
            want = {t: c for t, c in want.items() if c[0]}
            got = {t: c for t, c in trailing.counts(since, stop, hours).items() if c[0]}
            assert got == want
    assert cs.Trailing(CONFIG, []).first is None
