"""Rank-based 1-10 browser target scores and the difficulty-weighted overall (TARGET-SCORES-1;
browser pulse K-BP6, docs/index.html).

score() runs once per build, after every product window is built, over all product groups of
each window label, and again over the network summaries of payload v4 (score_networks()), the
windows the API reads. For each browser target and engine it ranks the networks with at least
MIN_REQUESTS completed tries of that engine on three outcome rates (success: `ok`, higher is
better; challenge and block, lower is better) and scores rank r of M as 1 + 9 x (M - r) / (M - 1),
ties sharing the average of their ranks. Engines are never mixed: Chrome tries never move a
Camoufox rank. Each engine's figures (browser.engines.<engine>) gain `scores`; the browser object
gains `scores`, a copy of its main engine's. Each window with browser targets gains
`browser_overall`: per engine, the success scores of the network's ranked targets weighted by
their difficulty (policy.browser_difficulty()), with a rank among the networks that have one.

Every difficulty is a FIXED value the owner set (2026-11-12): configs/methodology.toml
[browser_difficulty] and [http_difficulty], read as config, never derived from results (success
rates, ranks or outcomes); they change only on the owner's word. generic_http and
generic_https 1; amazon_pdp 2; instagram_web 3; google_search and cloudflare_challenge 4;
linkedin_public, ai_answers and its sources 5 (tests/test_target_difficulty.py).

SUCCESS-SCORE-CALC-1 (display only: no Index Score, Speed, rank or methodology id changes). Each
HTTP target with metrics (HTTP_TARGETS) gains `scores.success`, the same rule and shape on the
rate the API serves as its successRate (metrics.success_rate, over metrics.completed tries).
Each window with a scored target gains `success_overall` = {score, rank, ranked_of, status,
targets: {id: score}, weights: {id: weight}}: the network's ranked success scores of the HTTP
targets and of each browser target's main engine (browser.scores), counted as browser_overall
counts them, weighted by difficulty, an HTTP target's [http_difficulty] (policy.http_difficulty());
ranked as browser_overall is. Every existing field keeps its value.
"""

from __future__ import annotations

import copy
import math
from collections import defaultdict

from .policy import HTTP_DIFFICULTY, browser_difficulty, http_difficulty

# A network is ranked for a target only with this many completed tries of the engine in the
# window (the standard error of a rate near 50% is 5 points at 100 tries, 7 at 50).
MIN_REQUESTS = 100
# Fewer ranked networks score none: two would always be a 10 against a 1.
MIN_RANKED = 3
# A tight race: the spread between the best and worst ranked rate is under the larger of 3
# percentage points and 1.96 standard errors of that difference. Nobody is scored then.
TIGHT_RACE_MIN_PTS = 3.0
TIGHT_RACE_Z = 1.96
# Each score: the outcome rate (browser.OUTCOMES) it ranks and whether higher is better.
METRICS = {"success": ("ok", True), "challenge": ("challenge", False), "block": ("block", False)}
RANKED = "ranked"
NOT_ENOUGH_DATA = "not_enough_data"
TOO_FEW_NETWORKS = "too_few_networks"
ALL_SIMILAR = "all_similar"
# The overall's status when the network has no ranked target for the engine.
NO_RANKED_TARGETS = "no_ranked_targets"
# The targets scored on success alone, from the HTTP metrics; their weight in success_overall
# is the methodology's [http_difficulty].
HTTP_TARGETS = tuple(HTTP_DIFFICULTY)


def score(products, difficulty, groups, http=HTTP_DIFFICULTY):
    """Score every window of build()'s product groups, label by label (see the module).
    difficulty: policy.browser_difficulty(); groups: the methodology's [target_groups];
    http: policy.http_difficulty()."""
    by_label = defaultdict(list)
    for p in products:
        for label, window in p["windows"].items():
            by_label[label].append(window)
    sources = {s for members in groups.values() for s in members}
    # The overall counts each target with a difficulty once: a group's sources count through it.
    counted = {t: d for t, d in difficulty.items() if t not in sources}
    for windows in by_label.values():
        _target_scores(windows)
        _overall(windows, counted)
        _http_scores(windows)
        _success_overall(windows, counted | http)


def score_networks(networks, config):
    """score() of a build's network summaries (network.summaries()), each kind's networks
    ranked against each other per target, window and engine. config: the methodology."""
    kinds = defaultdict(list)
    for entry in networks:
        kinds[entry["kind"]].append(entry)
    difficulty = browser_difficulty(config)
    for peers in kinds.values():
        score(peers, difficulty, config.get("target_groups", {}), http_difficulty(config))


def _target_scores(windows):
    """Each engine's `scores` of every browser target in windows (one label), and each browser
    object's copy of its main engine's."""
    found = defaultdict(list)
    for window in windows:
        for target, entry in window["targets"].items():
            for name, own in entry.get("browser", {}).get("engines", {}).items():
                found[target, name].append(own)
    for engines in found.values():
        for own in engines:
            own["scores"] = {}
        for metric, (outcome, higher) in METRICS.items():
            rated = [(own["outcome_rates"][outcome], own["completed"]) for own in engines]
            for own, s in zip(engines, metric_scores(rated, higher), strict=True):
                own["scores"][metric] = s
    for window in windows:
        for entry in window["targets"].values():
            if "browser" in entry:
                b = entry["browser"]
                b["scores"] = copy.deepcopy(b["engines"][b["engine"]]["scores"])


def metric_scores(rated, higher):
    """The score object of each (rate, completed tries) of the networks of one target, engine
    and window, in order: {"rate", "score", "rank", "ranked_of", "status"}; score, rank and
    ranked_of are null unless the status is ranked."""
    enough = [i for i, (_, n) in enumerate(rated) if n >= MIN_REQUESTS]
    ranked = [rated[i] for i in enough]
    status = TOO_FEW_NETWORKS if len(ranked) < MIN_RANKED else RANKED
    if status == RANKED and similar(ranked):
        status = ALL_SIMILAR
    ranks = average_ranks([rate for rate, _ in ranked], higher) if status == RANKED else []
    found = [_score(rate, None, None, NOT_ENOUGH_DATA) for rate, _ in rated]
    for k, i in enumerate(enough):
        found[i] = _score(rated[i][0], ranks[k] if ranks else None, len(ranked), status)
    return found


def _score(rate, rank, of, status):
    if status != RANKED:
        return {"rate": rate, "score": None, "rank": None, "ranked_of": None, "status": status}
    value = 1 + 9 * (of - rank) / (of - 1)
    return {"rate": rate, "score": value, "rank": rank, "ranked_of": of, "status": status}


def similar(ranked):
    """Whether (rate, completed) of the ranked networks are a tight race: the best and worst
    rates (of each, the network with the most tries) are under the larger of
    TIGHT_RACE_MIN_PTS points and TIGHT_RACE_Z standard errors of their difference apart."""
    best = max(ranked, key=lambda x: (x[0], x[1]))
    worst = min(ranked, key=lambda x: (x[0], -x[1]))
    (p1, n1), (p2, n2) = best, worst
    error = math.sqrt(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2)
    return p1 - p2 < max(TIGHT_RACE_MIN_PTS / 100, TIGHT_RACE_Z * error)


def average_ranks(values, higher):
    """Each value's rank (1 the best; higher: the largest is best), ties sharing the average of
    their ranks."""
    order = sorted(range(len(values)), key=lambda i: values[i], reverse=higher)
    ranks = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start
        while end + 1 < len(order) and values[order[end + 1]] == values[order[start]]:
            end += 1
        for i in order[start : end + 1]:
            ranks[i] = (start + end) / 2 + 1
        start = end + 1
    return ranks


def _overall(windows, counted):
    """Each window's `browser_overall` per engine (windows with browser targets only), then
    each engine's rank among the windows that have an overall."""
    by_engine = defaultdict(list)
    for window in windows:
        engines = sorted(
            {
                name
                for entry in window["targets"].values()
                for name in entry.get("browser", {}).get("engines", {})
            }
        )
        if not engines:
            continue
        window["browser_overall"] = {name: overall(window, name, counted) for name in engines}
        for name in engines:
            if window["browser_overall"][name]["success"]["score"] is not None:
                by_engine[name].append(window["browser_overall"][name]["success"])
    for found in by_engine.values():
        _rank(found)


def _rank(found):
    """Each overall's rank (ties averaged) and ranked_of among found, the overalls with a score."""
    ranks = average_ranks([s["score"] for s in found], True)
    for s, rank in zip(found, ranks, strict=True):
        s["rank"], s["ranked_of"] = rank, len(found)


def overall(window, engine, counted):
    """One engine's overall of a window: the success scores of its ranked targets among
    `counted` (target: difficulty) weighted by difficulty, with the targets and weights used;
    rank and ranked_of are set by _overall()."""
    weights = {}
    total = 0.0
    for target, entry in window["targets"].items():
        own = entry.get("browser", {}).get("engines", {}).get(engine)
        if target in counted and own and own["scores"]["success"]["status"] == RANKED:
            weights[target] = counted[target]
            total += own["scores"]["success"]["score"] * counted[target]
    value = total / sum(weights.values()) if weights else None
    status = RANKED if weights else NO_RANKED_TARGETS
    success = {"score": value, "rank": None, "ranked_of": None, "status": status}
    return {"success": success, "targets": list(weights), "weights": weights}


def _http_scores(windows):
    """Each HTTP target's `scores.success` in windows (one label) where it has metrics: its
    success_rate ranked as a browser engine's ok rate."""
    for target in HTTP_TARGETS:
        found = [w["targets"][target] for w in windows if target in w["targets"]]
        found = [entry for entry in found if "completed" in entry["metrics"]]
        rated = [(e["metrics"]["success_rate"], e["metrics"]["completed"]) for e in found]
        for entry, s in zip(found, metric_scores(rated, True), strict=True):
            entry["scores"] = {"success": s}


def _success_overall(windows, weights):
    """Each window's `success_overall` (windows with a scored target only) over `weights`
    (target: weight): an HTTP target's own success score, a browser target's main engine's;
    then the rank among the windows that have a score."""
    found = []
    for window in windows:
        scored = {}
        for target, entry in window["targets"].items():
            own = entry["browser"]["scores"] if "browser" in entry else entry.get("scores")
            if target in weights and own:
                scored[target] = own["success"]
        if not scored:
            continue
        ranked = {t: s["score"] for t, s in scored.items() if s["status"] == RANKED}
        used = {t: weights[t] for t in ranked}
        value = sum(ranked[t] * used[t] for t in ranked) / sum(used.values()) if used else None
        status = RANKED if used else NO_RANKED_TARGETS
        overall = {"score": value, "rank": None, "ranked_of": None, "status": status}
        window["success_overall"] = overall | {"targets": ranked, "weights": used}
        if used:
            found.append(window["success_overall"])
    _rank(found)
