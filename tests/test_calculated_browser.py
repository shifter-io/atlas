"""Browser targets in the calculated snapshot (BP-CALC, browser pulse K-BP6 and D9): the
pure helpers that split, rate and list them, the target groups of the methodology and the
traffic snapshot. The PostgreSQL gate is in test_calculated_browser_gate.py.
"""

import copy
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest
from calculated_browser_cases import GROUP, SOURCES, row

from atlas import traffic
from atlas.browser import contracts
from atlas.calculated import browser, calculator
from atlas.calculated.aggregate import timed
from atlas.calculated.policy import load as load_policy
from atlas.calculated.policy import validate
from atlas.stats import Histogram

# --- pure -------------------------------------------------------------------------------------


def test_split_by_batch_kind_then_engine_keeps_order():
    rows = [
        {"target": "generic_http", "test": "pulse"},
        {"target": "amazon_pdp", "test": "browser"},
        {"target": "generic_https", "test": None},
        {"target": "google_search", "test": None},  # an unbatched run: the target's engine
        {"target": "ai_gemini", "test": None},  # K-BP1 browser target, not registered yet
        {"target": "generic_http", "test": "browser"},  # a browser batch, whatever the target
        {"target": "custom_echo", "test": "load"},  # unknown: http
    ]
    network, found = browser.split(rows)
    assert network == [rows[0], rows[2], rows[6]]
    assert found == [rows[1], rows[3], rows[4], rows[5]]
    assert browser.engine("ai_answers") == "browser" and browser.engine("custom_echo") == "http"


def test_outcome_rates_sum_to_one_and_match_the_counters():
    t = datetime(2026, 11, 4, tzinfo=UTC)
    rows = [
        row(
            t,
            20,
            8,
            errors={
                "challenge": 2,
                "blocked": 1,
                "login_wall": 3,
                "timeout": 1,
                "dns": 1,
                "tls": 4,
            },
            challenges=2,
            blocks=1,
            timeouts=1,
            proxy_errors=1,
        ),
        row(
            t,
            10,
            2,
            errors={"no_content": 2, "no_answer": 1, "tls": 3, "browser_error": 2},
        ),
    ]
    rates = browser.outcome_rates(rows)
    assert list(rates) == list(browser.OUTCOMES)
    assert rates == {
        "ok": 10 / 30,
        "challenge": 2 / 30,
        "block": 1 / 30,
        "login_wall": 3 / 30,
        "timeout": 1 / 30,
        "proxy_error": 1 / 30,
        "no_content": 2 / 30,
        "no_answer": 1 / 30,
        "other_error": 9 / 30,
    }
    assert abs(sum(rates.values()) - 1) < 1e-9
    # Nothing completed (no rows, or rows of no try): every rate is null.
    assert set(browser.outcome_rates([]).values()) == {None}
    assert set(browser.outcome_rates([row(t, 0, 0)]).values()) == {None}


def test_page_load_merges_the_success_histograms():
    t = datetime(2026, 11, 4, tzinfo=UTC)
    rows = [row(t, 5, 3, [(100, 2), (400, 1)]), row(t, 2, 1, [(1000, 1)]), row(t, 4, 0)]
    expected = Histogram(Counter({100: 2, 400: 1, 1000: 1})).percentiles()
    assert browser.page_load(rows) == expected
    assert expected == {"p50": 100, "p90": 1000, "p95": 1000, "p99": 1000}
    assert browser.page_load([row(t, 3, 0)]) == dict.fromkeys(("p50", "p90", "p95", "p99"))


def test_browser_targets_order_window_and_group():
    since = datetime(2026, 11, 4, tzinfo=UTC)
    end = since + timedelta(hours=24)
    rows = [
        row(since - timedelta(minutes=5), 1, 1, target="amazon_pdp"),  # before the window
        row(since, 2, 1, [(10, 1)], target="ai_gemini"),
        row(since, 3, 3, [(20, 3)], target="ai_overview"),
        row(since + timedelta(hours=1), 4, 0, target="zz_custom"),
        row(since + timedelta(hours=2), 5, 2, [(30, 2)], target="ai_gemini"),
        row(since + timedelta(hours=3), 6, 6, [(40, 6)], target="google_search"),
        row(end, 7, 7, target="google_search"),  # the window is half-open
    ]
    groups = {GROUP: SOURCES}
    found = browser.browser_targets(rows, since, end, groups)
    assert list(found) == ["google_search", GROUP, "ai_overview", "ai_gemini", "zz_custom"]
    assert found["google_search"] == [rows[5]]
    assert found["ai_gemini"] == [rows[1], rows[4]]
    # The group: its sources' rows in their original order (ai_chatgpt has none).
    assert found[GROUP] == browser.group_target(rows[1:6], SOURCES) == [rows[1], rows[2], rows[4]]
    assert browser.browser_targets(rows, since, end, {}).keys() == {
        "google_search",
        "ai_overview",
        "ai_gemini",
        "zz_custom",
    }
    assert browser.browser_targets(rows[:1], since, end, groups) == {}
    group = browser.fields(GROUP, found[GROUP], groups)
    assert (group["group"], group["sources"]) == (None, SOURCES)
    assert group["outcome_rates"]["ok"] == 6 / 10
    source = browser.fields("ai_gemini", found["ai_gemini"], groups)
    assert (source["group"], source["sources"]) == (GROUP, None)
    page = browser.fields("google_search", found["google_search"], groups)
    assert (page["group"], page["sources"]) == (None, None)
    assert page["page_load_ms"]["p50"] == 40


def test_targets_lists_network_targets_first_then_browser_targets():
    since = datetime(2026, 11, 4, tzinfo=UTC)
    network = [row(since, 1, 1, target="generic_https", test="pulse")]
    found = [row(since, 2, 1, target="ai_chatgpt")]
    config = {"required_targets": ["generic_http", "generic_https"], "target_groups": {}}
    listed = browser.targets(config, network, found, since, since + timedelta(hours=1))
    assert [(t, own, sorted(extra)) for t, own, extra in listed] == [
        ("generic_http", [], []),
        ("generic_https", network, []),
        ("ai_chatgpt", found, ["browser"]),
    ]
    assert listed[2][2]["browser"]["group"] is None  # no [target_groups]: no group


def test_methodology_target_groups():
    config, _ = load_policy("configs/methodology.toml")
    assert config["target_groups"] == {"social_web": ["instagram_web", "x_web"], GROUP: SOURCES}
    assert not set(config["required_targets"]) & set(contracts.BROWSER_TARGETS)
    for groups in (
        [],
        {GROUP: []},
        {GROUP: "ai_overview"},
        {GROUP: ["ai_overview", "ai_overview"]},
        {GROUP: ["ai_overview"], "other": ["ai_overview"]},
        {GROUP: [GROUP]},
        {GROUP: ["ai_overview"], "other": [GROUP]},
        {GROUP: [""]},
        {GROUP: [3]},
    ):
        with pytest.raises(ValueError, match="Invalid target groups"):
            validate(copy.deepcopy(config) | {"target_groups": groups})
    # Browser, group and source targets are per-target only: never required (D9 by construction).
    for target in ("amazon_pdp", GROUP, "ai_chatgpt"):
        required = copy.deepcopy(config)
        required["required_targets"] = [*config["required_targets"], target]
        required["benchmark_weights"] = config["benchmark_weights"] | {target: 1}
        with pytest.raises(ValueError, match="cannot be a required target"):
            validate(required)
    del config["target_groups"]
    assert validate(config)  # optional


def test_traffic_snapshot_keeps_the_browser_flag():
    by_test = {
        "pulse": traffic.totals([{"runs": 1, "requests": 2, "bytes_up": 3, "bytes_down": 4}]),
        "browser": traffic.totals([{"runs": 5, "requests": 6, "bytes_up": 7, "bytes_down": 8}])
        | {"bytes_exact": False},
    }
    assert traffic.snapshot(by_test) == {
        "pulse": {"runs": 1, "requests": 2, "bytes_up": 3, "bytes_down": 4},
        "browser": {
            "runs": 5,
            "requests": 6,
            "bytes_up": 7,
            "bytes_down": 8,
            "bytes_exact": False,
        },
    }
    with pytest.raises(ValueError, match="census, pulse, load or browser"):
        traffic.summary(None, traffic.TrafficInputs(test="other"))


def test_group_rate_is_timed_over_the_union():
    t = datetime(2026, 11, 4, tzinfo=UTC)
    rows = [
        row(t, 10, 10, [(1, 10)], target="ai_overview"),
        row(t + timedelta(hours=1), 10, 0, target="ai_chatgpt"),
        row(t, 30, 0, target="ai_gemini"),
        row(t, 50, 50, [(1, 50)], target="amazon_pdp"),  # no source: not in the group
    ]
    union = browser.group_target(rows, SOURCES)
    m = timed(union, 24, 1, slot_minutes=30)
    # Slot one: 10 of 40 succeeded; slot two: none of 10. Each slot weighs the same.
    assert m["success_rate"] == (10 / 40 + 0) / 2
    assert m["request_success_rate"] == 10 / 50


# --- engines (BROWSER-ENGINE-FIGURES-1) -------------------------------------------------------


def before_engines(target, rows, groups):
    """The K-BP6 browser object as it was before the engine split: every row summed."""
    return {
        "outcome_rates": browser.outcome_rates(rows),
        "page_load_ms": browser.page_load(rows),
        "group": next((g for g, sources in groups.items() if target in sources), None),
        "sources": list(groups[target]) if target in groups else None,
    }


def test_camoufox_only_windows_keep_every_figure():
    since = datetime(2026, 11, 4, tzinfo=UTC)
    rows = [
        row(since, 4, 3, [(100, 3)], {"tls": 1}, target="ai_overview"),
        row(since, 5, 1, [(300, 1)], {"no_answer": 4}, target="ai_chatgpt", engine="camoufox"),
        row(since + timedelta(hours=1), 6, 6, [(200, 6)], target="amazon_pdp"),
    ]
    config = {"required_targets": [], "target_groups": {GROUP: SOURCES}}
    listed = browser.targets(config, [], rows, since, since + timedelta(hours=2))
    found = browser.browser_targets(rows, since, since + timedelta(hours=2), {GROUP: SOURCES})
    assert [(t, own) for t, own, _ in listed] == list(found.items())  # every row, as before
    for target, own, extra in listed:
        b = extra["browser"]
        assert {k: b[k] for k in list(b)[:4]} == before_engines(target, own, {GROUP: SOURCES})
        assert b["engine"] == "camoufox" and list(b["engines"]) == ["camoufox"]
        assert b["engines"]["camoufox"] == {
            "completed": sum(r["counters"]["completed"] for r in own),
            "successes": sum(r["counters"]["successes"] for r in own),
            "outcome_rates": b["outcome_rates"],
            "page_load_ms": b["page_load_ms"],
        }
    assert list(listed[0][2]["browser"]) == [
        *("outcome_rates", "page_load_ms", "group", "sources"),
        *("engine", "engines"),
    ]


def test_two_engines_are_never_added_together():
    since = datetime(2026, 11, 4, tzinfo=UTC)
    camoufox = [row(since, 10, 9, [(100, 9)], {"tls": 1}), row(since, 2, 0, engine="camoufox")]
    chrome = [
        row(since, 20, 5, [(900, 5)], {"challenge": 15}, challenges=15, engine="chrome"),
        row(since + timedelta(hours=1), 1, 1, [(800, 1)], engine="chrome"),
    ]
    rows = [camoufox[0], chrome[0], camoufox[1], chrome[1]]
    config = {"required_targets": [], "target_groups": {}}
    ((target, own, extra),) = browser.targets(config, [], rows, since, since + timedelta(hours=2))
    b = extra["browser"]
    assert (target, own, b["engine"]) == ("amazon_pdp", chrome, "chrome")  # 21 tries beat 12
    assert b["outcome_rates"] == browser.outcome_rates(chrome)
    assert b["outcome_rates"]["challenge"] == 15 / 21
    assert b["page_load_ms"] == browser.page_load(chrome)
    assert list(b["engines"]) == ["camoufox", "chrome"]
    assert b["engines"]["camoufox"] == browser.engine_fields(camoufox)
    assert b["engines"]["camoufox"]["completed"] == 12
    assert b["engines"]["chrome"]["successes"] == 6
    assert b["engines"]["camoufox"]["page_load_ms"]["p50"] == 100
    # The first hour alone: 20 Chrome tries against 12 Camoufox ones; a tie goes to Camoufox.
    ((_, own, extra),) = browser.targets(config, [], rows, since, since + timedelta(hours=1))
    assert own == [chrome[0]] and extra["browser"]["engine"] == "chrome"
    tie = [row(since, 3, 3, engine="chrome"), row(since, 3, 0)]
    ((_, own, extra),) = browser.targets(config, [], tie, since, since + timedelta(hours=1))
    assert own == [tie[1]] and extra["browser"]["engine"] == "camoufox"
    assert browser.lead_engine({}) == "camoufox"
    assert browser.fields("amazon_pdp", [], {})["engines"] == {}


def test_a_group_target_is_split_per_engine_over_its_sources():
    since = datetime(2026, 11, 4, tzinfo=UTC)
    rows = [
        row(since, 4, 4, [(100, 4)], target="ai_overview"),
        row(since, 6, 3, [(500, 3)], target="ai_chatgpt", engine="chrome"),
        row(since, 2, 1, [(200, 1)], target="ai_gemini", engine="camoufox"),
        row(since, 3, 0, target="ai_gemini", engine="chrome"),
    ]
    config = {"required_targets": [], "target_groups": {GROUP: SOURCES}}
    listed = browser.targets(config, [], rows, since, since + timedelta(hours=1))
    found = {t: (own, extra["browser"]) for t, own, extra in listed}
    own, group = found[GROUP]
    assert group["engine"] == "chrome" and own == [rows[1], rows[3]]  # 9 tries against 6
    assert group["engines"]["chrome"] == browser.engine_fields([rows[1], rows[3]])
    assert group["engines"]["camoufox"] == browser.engine_fields([rows[0], rows[2]])
    assert group["outcome_rates"]["ok"] == 3 / 9 and group["sources"] == SOURCES
    # Each source keeps its own lead: ai_gemini's Chrome tries (3) beat its Camoufox ones (2).
    assert found["ai_gemini"][0] == [rows[3]] and found["ai_overview"][1]["engine"] == "camoufox"


def test_the_social_group_is_chromes_whenever_chrome_ran_it():
    """VALUE-FAIL-1: X ran on Camoufox before 2026-11-16 (SOCIAL-ENGINE-1); those tries never
    take social_web over from Chrome, which runs both Instagram and X since."""
    since = datetime(2026, 11, 4, tzinfo=UTC)
    rows = [
        row(since, 4, 4, [(100, 4)], target="x_web"),
        row(since, 3, 3, [(100, 3)], target="x_web"),
        row(since, 2, 1, [(500, 1)], target="instagram_web", engine="chrome"),
    ]
    config = {"required_targets": [], "target_groups": {"social_web": ["instagram_web", "x_web"]}}
    listed = browser.targets(config, [], rows, since, since + timedelta(hours=1))
    found = {t: (own, extra["browser"]) for t, own, extra in listed}
    own, group = found["social_web"]
    assert group["engine"] == "chrome" and own == [rows[2]]  # 2 tries against Camoufox's 7
    assert found["x_web"][1]["engine"] == "camoufox"  # a source keeps the most tries
    camoufox_only = {"camoufox": {"completed": 5}, "chrome": {"completed": 0}}
    assert browser.lead_engine(camoufox_only, "social_web") == "camoufox"
    assert browser.lead_engine({"camoufox": {"completed": 5}}, "social_web") == "camoufox"
    both = {"camoufox": {"completed": 5}, "chrome": {"completed": 2}}
    assert browser.lead_engine(both, "amazon_pdp") == "camoufox"


def test_each_row_carries_its_runs_engine():
    run = {
        "scope": {"target": {"id": "amazon_pdp"}, "config": {"browser_engine": "chrome"}},
        "role": "browser",
        "test": "browser",
        "light": False,
        "finished_at": datetime(2026, 11, 4, tzinfo=UTC),
    }
    assert calculator._run_fields(run, [], 1800).fields["engine"] == "chrome"
    del run["scope"]["config"]["browser_engine"]  # a run from before the engine choice
    assert calculator._run_fields(run, [], 1800).fields["engine"] == "camoufox"
    assert browser.run_engine({"browser_engine": None}) == "camoufox"
