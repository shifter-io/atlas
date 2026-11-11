"""Rank-based browser target scores and the difficulty-weighted overall (TARGET-SCORES-1,
calculated/target_scores.py): the worked example of the issue, the data, network-count and
tight-race statuses, engine separation, the overall's weights, the methodology table and the
byte-identical existing fields (a PostgreSQL build with and without the scores)."""

import copy
import json

import pytest
from calculated_browser_cases import GROUP, SOURCES, add_browser_runs, first_difference, generic
from calculated_cases import POLICY, lenient_policy
from psycopg.rows import dict_row

from proxy_index.calculated import calculator, incremental
from proxy_index.calculated import target_scores as ts
from proxy_index.calculated.inputs import cutoff
from proxy_index.calculated.policy import browser_difficulty, load, validate
from proxy_index.storage import connect

pytest_plugins = ["test_storage", "calculated_browser_cases"]

GROUPS = {GROUP: SOURCES}
WEIGHTS = {"amazon_pdp": 2, "google_search": 3, "ai_overview": 4, "ai_gemini": 3, GROUP: 4}


def own(ok, completed, challenge=0.0, block=0.0):
    """One engine's figures of a target (browser.engine_fields()), rates only."""
    return {
        "completed": completed,
        "successes": round(ok * completed),
        "outcome_rates": {"ok": ok, "challenge": challenge, "block": block},
        "page_load_ms": {"p50": 100},
    }


def window(targets):
    """A product window with browser targets: {target: {engine: own()}}, its main engine
    Camoufox when it has one."""
    found = {"generic_http": {"metrics": {}, "score": None}}
    for target, engines in targets.items():
        lead = "camoufox" if "camoufox" in engines else next(iter(engines))
        browser = {"outcome_rates": engines[lead]["outcome_rates"], "engine": lead}
        found[target] = {"metrics": {}, "browser": browser | {"engines": engines}}
    return {"targets": found}


def products(*windows):
    return [{"windows": {"24h": w}} for w in windows]


def success(w, target, engine="camoufox"):
    return w["targets"][target]["browser"]["engines"][engine]["scores"]["success"]


# --- the worked example -----------------------------------------------------------------------


def test_worked_example_ranks_ties_and_scores():
    rated = [(0.90, 500), (0.80, 500), (0.80, 500), (0.50, 500)]
    found = ts.metric_scores(rated, True)
    assert [s["rank"] for s in found] == [1, 2.5, 2.5, 4]
    assert [s["score"] for s in found] == [10, 5.5, 5.5, 1]
    assert {s["status"] for s in found} == {ts.RANKED} and {s["ranked_of"] for s in found} == {4}
    assert [s["rate"] for s in found] == [r for r, _ in rated]
    assert list(found[0]) == ["rate", "score", "rank", "ranked_of", "status"]
    # Lower is better (challenge, block): the order turns round.
    assert [s["score"] for s in ts.metric_scores(rated, False)] == [1, 5.5, 5.5, 10]
    assert ts.average_ranks([0.5, 0.5, 0.5], True) == [2, 2, 2]


def test_worked_example_overall():
    a = window(
        {
            "amazon_pdp": {"camoufox": own(0.90, 500)},
            "google_search": {"camoufox": own(0.80, 500)},
        }
    )
    peers = [
        window({t: {"camoufox": own(rate, 500)} for t in ("amazon_pdp", "google_search")})
        for rate in (0.80, 0.80, 0.50)
    ]
    # google_search: A 0.8 beside 0.9, 0.7 and 0.4 below it, so A ranks 2 of 4: 1 + 9 x 2/3 = 7.
    peers[0]["targets"]["google_search"]["browser"]["engines"]["camoufox"] = own(0.90, 500)
    peers[1]["targets"]["google_search"]["browser"]["engines"]["camoufox"] = own(0.70, 500)
    peers[2]["targets"]["google_search"]["browser"]["engines"]["camoufox"] = own(0.40, 500)
    ts.score(products(a, *peers), WEIGHTS, GROUPS)
    assert success(a, "amazon_pdp")["score"] == 10 and success(a, "google_search")["score"] == 7
    overall = a["browser_overall"]["camoufox"]
    assert overall["success"]["score"] == pytest.approx((10 * 2 + 7 * 3) / 5) == 8.2
    assert overall["targets"] == ["amazon_pdp", "google_search"]
    assert overall["weights"] == {"amazon_pdp": 2, "google_search": 3}
    # The first peer: 5.5 on amazon_pdp, 10 on google_search, so (5.5 x 2 + 10 x 3) / 5 = 8.2 too.
    assert (overall["success"]["rank"], overall["success"]["ranked_of"]) == (1.5, 4)
    assert [w["browser_overall"]["camoufox"]["success"]["rank"] for w in peers] == [1.5, 3, 4]
    assert overall["success"]["status"] == ts.RANKED
    # The browser object's scores are its main engine's.
    assert a["targets"]["amazon_pdp"]["browser"]["scores"]["success"] == success(a, "amazon_pdp")
    assert "browser_overall" in peers[2] and "scores" not in a["targets"]["generic_http"]


# --- statuses ---------------------------------------------------------------------------------


def test_not_enough_data_below_one_hundred_tries():
    rated = [(0.9, 100), (0.8, 100), (0.5, 100), (0.95, 99)]
    found = ts.metric_scores(rated, True)
    assert found[3] == {
        "rate": 0.95,
        "score": None,
        "rank": None,
        "ranked_of": None,
        "status": ts.NOT_ENOUGH_DATA,
    }
    assert [s["rank"] for s in found[:3]] == [1, 2, 3] and found[0]["ranked_of"] == 3
    assert ts.MIN_REQUESTS == 100
    # A network with no completed try has a null rate.
    assert ts.metric_scores([(None, 0)], True)[0]["status"] == ts.NOT_ENOUGH_DATA


def test_too_few_networks_at_two():
    found = ts.metric_scores([(0.9, 500), (0.1, 500), (0.5, 20)], True)
    assert [s["status"] for s in found] == [ts.TOO_FEW_NETWORKS] * 2 + [ts.NOT_ENOUGH_DATA]
    assert {s["score"] for s in found} == {None} and found[0]["rate"] == 0.9
    assert ts.MIN_RANKED == 3


def test_all_similar_under_three_points():
    # 100,000 tries: the standard error is far below 3 points, so 3 points decide.
    close = [(0.800, 100_000), (0.790, 100_000), (0.771, 100_000)]
    apart = [(0.800, 100_000), (0.790, 100_000), (0.769, 100_000)]
    assert {s["status"] for s in ts.metric_scores(close, True)} == {ts.ALL_SIMILAR}
    assert {s["score"] for s in ts.metric_scores(close, True)} == {None}
    assert {s["status"] for s in ts.metric_scores(apart, True)} == {ts.RANKED}


def test_all_similar_under_the_standard_error_bound():
    # 100 tries near 50%: 1.96 x sqrt(2 x 0.25 / 100) = 13.9 points.
    close = [(0.565, 100), (0.5, 100), (0.43, 100)]
    apart = [(0.575, 100), (0.5, 100), (0.43, 100)]
    assert ts.similar(close) and not ts.similar(apart)
    assert {s["status"] for s in ts.metric_scores(close, True)} == {ts.ALL_SIMILAR}
    assert {s["status"] for s in ts.metric_scores(apart, False)} == {ts.RANKED}
    # Tied extremes: the one with the most tries counts (the smaller error).
    assert not ts.similar([(0.575, 100), (0.575, 10_000), (0.43, 100), (0.43, 10_000)])


# --- engines and the overall ------------------------------------------------------------------


def camoufox_ranks(windows):
    return [
        [
            success(w, t)
            for t in w["targets"]
            if "camoufox" in w["targets"][t].get("browser", {}).get("engines", {})
        ]
        for w in windows
    ]


def test_chrome_rows_change_no_camoufox_rank():
    rates = (0.9, 0.7, 0.5, 0.3)
    plain = [window({"amazon_pdp": {"camoufox": own(r, 300)}}) for r in rates]
    mixed = copy.deepcopy(plain)
    # Chrome: the order turned round, plus a fifth network measured with Chrome only.
    for w, r in zip(mixed, reversed(rates), strict=True):
        w["targets"]["amazon_pdp"]["browser"]["engines"]["chrome"] = own(r, 5000)
    mixed.append(window({"amazon_pdp": {"chrome": own(0.99, 5000)}}))
    ts.score(products(*plain), WEIGHTS, GROUPS)
    ts.score(products(*mixed), WEIGHTS, GROUPS)
    assert camoufox_ranks(mixed[:4]) == camoufox_ranks(plain)
    assert [success(w, "amazon_pdp")["rank"] for w in plain] == [1, 2, 3, 4]
    assert [success(w, "amazon_pdp", "chrome")["rank"] for w in mixed] == [5, 4, 3, 2, 1]
    assert success(mixed[4], "amazon_pdp", "chrome")["ranked_of"] == 5
    assert [w["browser_overall"]["camoufox"] for w in mixed[:4]] == [
        w["browser_overall"]["camoufox"] for w in plain
    ]
    assert list(mixed[0]["browser_overall"]) == ["camoufox", "chrome"]
    assert list(mixed[4]["browser_overall"]) == ["chrome"]
    # The Chrome-only network's main engine is Chrome.
    assert mixed[4]["targets"]["amazon_pdp"]["browser"]["scores"]["success"]["rank"] == 1


def test_overall_counts_groups_once_and_leaves_out_unranked_targets():
    def network(rate, cloudflare):
        return window(
            {
                "amazon_pdp": {"camoufox": own(rate, 500)},
                "google_search": {"camoufox": own(0.5, 500)},  # all similar
                "cloudflare_challenge": {"camoufox": own(rate, cloudflare)},
                GROUP: {"camoufox": own(rate, 600)},
                "ai_overview": {"camoufox": own(rate, 200)},
                "ai_gemini": {"camoufox": own(rate, 200)},
            }
        )

    found = [network(0.9, 500), network(0.6, 500), network(0.3, 50)]
    ts.score(products(*found), WEIGHTS | {"cloudflare_challenge": 4}, GROUPS)
    # cloudflare_challenge: two networks with enough data.
    assert success(found[0], "cloudflare_challenge")["status"] == ts.TOO_FEW_NETWORKS
    assert success(found[2], "cloudflare_challenge")["status"] == ts.NOT_ENOUGH_DATA
    assert success(found[0], "google_search")["status"] == ts.ALL_SIMILAR
    assert success(found[0], "ai_overview")["status"] == ts.RANKED  # ranked, not counted
    best = found[0]["browser_overall"]["camoufox"]
    assert best["targets"] == ["amazon_pdp", GROUP]
    assert best["weights"] == {"amazon_pdp": 2, GROUP: 4}
    assert best["success"]["score"] == 10
    assert [w["browser_overall"]["camoufox"]["success"]["score"] for w in found] == [10, 5.5, 1]
    # No ranked target: no overall, and no rank.
    lonely = window({"amazon_pdp": {"camoufox": own(0.9, 10)}})
    ts.score(products(lonely), WEIGHTS, GROUPS)
    assert lonely["browser_overall"]["camoufox"] == {
        "success": {"score": None, "rank": None, "ranked_of": None, "status": "no_ranked_targets"},
        "targets": [],
        "weights": {},
    }
    # A window without browser targets gets no overall.
    bare = {"targets": {"generic_http": {"metrics": {}}}}
    ts.score(products(bare), WEIGHTS, GROUPS)
    assert bare == {"targets": {"generic_http": {"metrics": {}}}}


def test_every_window_label_is_ranked_on_its_own():
    day = [window({"amazon_pdp": {"camoufox": own(r, 500)}}) for r in (0.9, 0.6, 0.3)]
    week = [window({"amazon_pdp": {"camoufox": own(r, 500)}}) for r in (0.3, 0.6, 0.9)]
    ts.score(
        [{"windows": {"24h": d, "7d": w}} for d, w in zip(day, week, strict=True)], WEIGHTS, {}
    )
    assert [success(w, "amazon_pdp")["rank"] for w in day] == [1, 2, 3]
    assert [success(w, "amazon_pdp")["rank"] for w in week] == [3, 2, 1]


# --- the methodology --------------------------------------------------------------------------


def test_the_shipped_difficulty_and_the_group_rule():
    config, _ = load(POLICY)
    assert browser_difficulty(config) == {
        "amazon_pdp": 2,
        "google_search": 3,
        "linkedin_public": 3,
        "instagram_web": 4,
        "cloudflare_challenge": 4,
        "ai_overview": 4,
        "ai_chatgpt": 4,
        "ai_gemini": 3,
        GROUP: 4,  # the highest of its sources
    }
    stated = copy.deepcopy(config)
    stated["browser_difficulty"][GROUP] = 1
    assert browser_difficulty(validate(stated))[GROUP] == 1  # a group's own value wins
    del stated["browser_difficulty"]
    assert validate(stated) and browser_difficulty(stated) == {}  # optional


@pytest.mark.parametrize(
    "change",
    [
        {"amazon_pdp": None},  # a run target without a value
        {"amazon_pdp": 0},
        {"amazon_pdp": 6},
        {"amazon_pdp": 2.5},
        {"amazon_pdp": True},
        {"generic_http": 3},  # not a browser target
    ],
)
def test_the_methodology_refuses_a_missing_or_invalid_difficulty(change):
    config, _ = load(POLICY)
    for target, value in change.items():
        if value is None:
            del config["browser_difficulty"][target]
        else:
            config["browser_difficulty"][target] = value
    with pytest.raises(ValueError, match="Invalid browser difficulty"):
        validate(config)
    with pytest.raises(ValueError, match="Invalid browser difficulty"):
        validate(load(POLICY)[0] | {"browser_difficulty": [2]})


# --- byte-identical existing fields -----------------------------------------------------------


def without_scores(payload):
    """The payload without the new fields (also in a build's network windows and their "_lead",
    the lead product's window, which network_scores.py drops before publishing)."""
    payload = copy.deepcopy(payload)
    windows = [w for p in payload["products"] for w in p["windows"].values()]
    for n in payload.get("networks", []):
        windows += [x for w in n["windows"].values() for x in (w, w["_lead"])]
    for w in windows:
        w.pop("browser_overall", None)
        for entry in w.get("targets", {}).values():
            if "browser" in entry:
                entry["browser"].pop("scores")
                for figures in entry["browser"]["engines"].values():
                    figures.pop("scores")
    return payload


def test_scoring_changes_no_existing_field():
    found = [
        window({"amazon_pdp": {"camoufox": own(r, 500)}, "ai_gemini": {"camoufox": own(r, 50)}})
        for r in (0.9, 0.6, 0.3)
    ]
    before = {"products": copy.deepcopy(products(*found))}
    after = {"products": products(*found)}
    ts.score(after["products"], WEIGHTS, GROUPS)
    assert json.dumps(without_scores(after)) == json.dumps(before)
    assert json.dumps(after) != json.dumps(before)


def built(database, policy, as_of):
    """build() of the stored facts at as_of (the full calculation), as JSON, without its
    per-build value."""
    config, method_id = load(policy)
    with connect(database) as conn:
        conn.row_factory = dict_row
        inputs = incremental.read(conn, config, method_id, cutoff(conn, as_of, False), "full")
    payload = calculator.build(inputs, config, method_id, 1, None)
    del payload["generated_at"]
    return json.loads(json.dumps(payload, default=str))


@pytest.mark.asyncio
async def test_a_camoufox_build_is_byte_identical_apart_from_the_scores(
    database, tmp_path, ai_sources, monkeypatch
):
    """Three products x eight run targets of Camoufox runs (calculated_browser_cases), built with
    and without the scores. Every network ties on every target, so with the tight-race bounds
    at zero each one ranks 2 of 3 (5.5) and has an overall."""
    as_of = await generic(database, tmp_path)
    await add_browser_runs(database, tmp_path)
    policy = lenient_policy(tmp_path)
    monkeypatch.setattr(ts, "MIN_REQUESTS", 1)
    monkeypatch.setattr(ts, "TIGHT_RACE_MIN_PTS", 0.0)
    monkeypatch.setattr(ts, "TIGHT_RACE_Z", 0.0)
    after = built(database, policy, as_of)
    with monkeypatch.context() as m:
        m.setattr(ts, "score", lambda *args: None)
        before = built(database, policy, as_of)
    stripped = without_scores(after)
    # The first difference only: a diff of two whole payloads takes minutes.
    assert first_difference(stripped, before) is None
    for p in after["products"]:
        w = p["windows"]["24h"]
        assert w["targets"]["amazon_pdp"]["browser"]["scores"]["success"]["score"] == 5.5
        overall = w["browser_overall"]["camoufox"]
        assert overall["success"] == {"score": 5.5, "rank": 2, "ranked_of": 3, "status": "ranked"}
        assert overall["targets"] == [
            "amazon_pdp",
            "google_search",
            "linkedin_public",
            "instagram_web",
            "cloudflare_challenge",
            GROUP,
        ]
