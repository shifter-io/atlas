"""COVERAGE-ALERT-1 (#1216, part B): with [value] browser_window = "7d" the 24h bill prices its
browser card targets over the last 7 days, and one without a run in the 24 hours is carried over
(stale, with its age); with none in 7 days it is missing again."""

import copy
from datetime import UTC, datetime, timedelta

import pytest
from calculated_cases import build_inputs
from test_calculated_scores import peer_inputs

from atlas.calculated.policy import digest, load, value_basis
from atlas.calculated.scores import component
from atlas.calculated.value_all import (
    NO_RUN,
    by_rank_all,
    card_targets,
    reprice,
    reprice_windows,
    target_costs,
    wider,
)

CONFIG, METHOD_ID = load("configs/methodology.toml")
CARDS = card_targets(CONFIG)
BROWSER = CARDS[2:]
AS_OF = datetime(2027, 1, 10, 12, tzinfo=UTC)
# $2 per 1,000 successes at $2/GB; the 7d window's browser runs cost twice as much.
DAY = {"completed": 1000, "successes": 500, "bytes_up": 1e8, "bytes_down": 4e8}
WEEK = DAY | {"bytes_down": 9e8}


def window(metrics, cards, age_hours=1):
    last = AS_OF - timedelta(hours=age_hours)
    return {t: {"metrics": metrics | {"last_observed": last}} for t in cards}


def value(targets, price=2.0):
    return component(price, inputs={"all_targets": target_costs(price, targets, CONFIG)})


def test_the_methodology_prices_the_browser_targets_over_seven_days():
    assert CONFIG["value"]["browser_window"] == "7d"
    assert (wider("24h", CONFIG), wider("7d", CONFIG), wider("30d", CONFIG)) == ("7d", None, None)
    assert wider("24h", copy.deepcopy(CONFIG) | {"value": {"basis": "rank"}}) is None
    for bad in ("24h", "2d", 7):
        with pytest.raises(ValueError):
            value_basis({"value": {"basis": "rank", "browser_window": bad}})
    assert digest(copy.deepcopy(CONFIG) | {"value": {"basis": "rank_all_targets"}}) != METHOD_ID


def test_a_browser_target_last_run_three_days_ago_is_priced_and_carried_over():
    day = window(DAY, CARDS[:2]) | window(DAY, BROWSER[1:])  # no amazon_pdp in 24 hours
    week = window(WEEK, CARDS) | window(WEEK, BROWSER[:1], age_hours=72)
    v = value(day)
    assert v["inputs"]["all_targets"]["amazon_pdp"] == [None, NO_RUN]
    reprice(v, day, week, CONFIG, AS_OF)
    costs = v["inputs"]["all_targets"]
    assert costs["generic_http"][0] == pytest.approx(2.0)  # HTTP: its own 24 hours
    assert {t: costs[t][0] for t in BROWSER} == {t: pytest.approx(4.0) for t in BROWSER}
    assert v["inputs"]["carried_over_all_targets"] == {"amazon_pdp": 72.0}
    [scored] = by_rank_all([v], 1)
    assert scored["value"] == 100 and scored["inputs"]["rank"] == 1
    assert scored["inputs"]["stale"] is True
    assert scored["inputs"]["carried_over_all_targets"] == {"amazon_pdp": 72.0}
    assert scored["inputs"]["missing_targets_all_targets"] == []


def test_a_browser_target_last_run_eight_days_ago_is_missing():
    day = window(DAY, CARDS[:2]) | window(DAY, BROWSER[1:])
    week = window(WEEK, CARDS[:2]) | window(WEEK, BROWSER[1:])  # amazon_pdp aged out
    v = value(day)
    reprice_windows({"24h": {"targets": day}, "7d": {"targets": week}}, {"24h": v}, CONFIG, AS_OF)
    assert v["inputs"]["carried_over_all_targets"] == {}
    [scored] = by_rank_all([v], 1)
    assert scored["value"] is None and scored["inputs"]["stale"] is False
    assert scored["inputs"]["missing_targets_all_targets"] == ["amazon_pdp"]


def test_a_network_with_every_target_in_24_hours_is_not_flagged():
    day, week = window(DAY, CARDS), window(WEEK, CARDS)
    v, wide = value(day), value(week)
    windows = {"24h": {"targets": day}, "7d": {"targets": week}}
    reprice_windows(windows, {"24h": v, "7d": wide}, CONFIG, AS_OF)
    assert v["inputs"]["carried_over_all_targets"] == {}
    assert by_rank_all([v], 1)[0]["inputs"]["stale"] is False
    # The 7d window keeps its own figures.
    assert "carried_over_all_targets" not in wide["inputs"]


def test_the_index_score_of_a_network_with_a_carried_over_target_exists():
    """A build: three networks whose one browser card target (x_web) ran three days ago only;
    their 24h value is priced from it, flagged stale, and their score, grade and rank exist."""
    from atlas.calculated.calculator import build

    c, now, runs, buckets, observations, evidence = peer_inputs()
    c["value"] = {"basis": "rank_all_targets", "browser_window": "7d"}
    c["browser_difficulty"], c["target_groups"] = {"x_web": 1}, {}
    for bucket in buckets:
        bucket["counters"] |= {"bytes_up": 100, "bytes_down": 900}
    back = timedelta(days=3)
    for run in [r for r in runs if r["scope"]["target"]["id"] == "generic_https"]:
        key = run["run_key"] + 5
        scope = copy.deepcopy(run["scope"])
        scope["target"] = {"id": "x_web", "url": "https://x.com/", "version": "1"}
        runs.append(
            run
            | {"run_key": key, "run_id": str(key), "batch_id": key, "test": "browser"}
            | {"scope": scope, "slot_at": run["slot_at"] - back}
            | {"started_at": run["started_at"] - back, "finished_at": run["finished_at"] - back}
        )
        bucket = next(b for b in buckets if b["run_key"] == run["run_key"])
        buckets.append(bucket | {"run_key": key, "bucket_start": bucket["bucket_start"] - back})
    result = build(build_inputs(runs, buckets, observations, evidence, now), c, digest(c), 1, None)
    for product in result["products"]:
        score = product["windows"]["24h"]["scores"]["balanced"]
        inputs = score["components"]["value"]["inputs"]
        assert inputs["carried_over_all_targets"] == {"x_web": pytest.approx(72, abs=1)}
        assert inputs["stale"] is True and inputs["rank"] is not None
        assert score["score"] is not None and score["grade"] is not None
    assert {p["windows"]["24h"]["scores"]["balanced"]["rank"] for p in result["products"]} == {
        1,
        2,
        3,
    }
