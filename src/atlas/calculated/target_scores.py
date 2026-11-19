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
generic_https 1; amazon_pdp 2; social_web and its sources instagram_web and x_web 3;
google_search and cloudflare_challenge 4; linkedin_public, ai_answers and its sources 5
(tests/test_target_difficulty.py). An overall counts each group once, never its sources.

SUCCESS-SCORE-CALC-1 (display only: no Index Score, Speed, rank or methodology id changes). Each
HTTP target with metrics (HTTP_TARGETS) gains `scores.success`, the same rule and shape on the
rate the API serves as its successRate (metrics.success_rate, over metrics.completed tries).
Every existing field keeps its value.

SUCCESS-ALL-8-1 (the owner, 2026-11-19; display only: the Atlas Score's reliability reads each
target's metrics.success_rate, never this). Each network window gains `success_overall` = {score,
rank, ranked_of, status, targets: {id: score}, weights: {id: weight}, missing_targets, basis},
the Success x/10, over the card targets (success_overall(); the window's "_success",
card_success.counts(): the tries the all-targets bill prices, a 24h window's browser targets over
the 7 days). A target's rate is its successes over its completed tries in the window; its score
is its distance from the best rate of the networks of the kind with tries on it in the same
window: max(floor, 10 - slope x the gap in percentage points), unrounded, the methodology's
[success_score] (policy.success_score()), so the best reads 10 and equal rates score the same.
The score is the mean of the target scores weighted by their fixed difficulty
(card_success.weights()), plus one offset per kind and window so the best network reads 10 (the
API's SUCCESS-SHIFT-1 rule, applied here so a window, its days and its chart points agree), ranked
(ties averaged) among the networks of the kind that have one. A target with no try in its window
is left out (the weights renormalised over the others) and named in missing_targets, status
partial_targets (as DAY-SCORE-1 counted a day's targets present);
without any, the score, rank and ranked_of are null, status no_ranked_targets. basis is the
methodology's, so a delta never compares two rules (previous_scores()). It replaced the daily
tests' generic_http alone (SUCCESS-DAILY-DISTANCE-1, 2026-11-16).

SUCCESS-POINTS-1 (#1301, the owner, 2026-11-17; display only). Each network window's series point
gains `success_score` = {score, status}: the same rule over the card targets of the TRAILING window
of the window's length ending at the point's end (point_overall(); network.point_trailing(),
SUCCESS-TRAILING-1), the best network the kind's best over that same window, so the newest point
is the window's own score and a steady network's line reads its headline. (A point over its own
hour or day alone was too noisy: an hour holds the few countries whose daily test ran then.)

DAY-SCORE-1 (display only). score_days() scores each network's days (network.Network.days(),
each day's targets and card targets measured as a 24h window's) as score_networks() scores its
windows: per kind, the networks' same day against each other. Each day keeps its start and its
success_overall without weights, so a day and a one-day window agree, and the last day (the 24h
window's span) is the 24h window's success_overall.

SUCCESS-SCORE-DELTA-1 (display only). previous_scores() runs once per build on the completed
network summaries (network.attach()): each window's success_overall and each target's success
score (an HTTP target's scores.success, a browser target's browser.scores.success) gain
`previous_score`, the previous published build's score of the same network, window and target,
as retain() keeps previous_rank; null without one, and success_overall's null when the previous
build's basis is not this one's (the first build of a new rule, SUCCESS-ALL-8-1). The API serves
score minus previous_score.

CHALLENGE-BLOCK-TIE-1 (display only: challenge and block feed no success overall). A tight race on
challenge or block is a shared first place: every ranked network scores 10, rank 1 of M, status
ranked (success kept all_similar until SUCCESS-TIE-1). Each HTTP target with metrics also gains
`scores.challenge` and `scores.block`, its challenge_rate and block_rate ranked as a browser
engine's.

CHALLENGE-OVERALL-1 (display only, SUCCESS-RATE-REMOVE-1). Each window with a scored target also
gains `challenge_overall` = {score, rank, ranked_of, status, targets, weights}: the ranked
challenge scores of the HTTP targets (scores.challenge) and of each browser target's main engine
(browser.scores.challenge), counted as browser_overall counts them, weighted by difficulty (an
HTTP target's [http_difficulty], policy.http_difficulty()), ranked as browser_overall is; 10 is
the fewest challenges. It feeds no Index Score, Speed, rank, success score or previous_score.

SUCCESS-TIE-1 (the owner, 2026-11-12). A tight race on success is a shared first place too: no
target score is ever all_similar. A tied target now counts in browser_overall as a 10 at its
difficulty; the Index Score and Speed read neither.

SCORE-BROWSER-MIN-1 (the owner, 2026-11-15). A browser target's engine is ranked for a network only
with the methodology's browser_min_requests completed tries in the window (100, one run, since
BROWSER-MIN-100-1, the owner's 2026-11-16; was 500; policy.browser_min_requests()), each window
its own, of whole runs (BROWSER-WHOLE-RUN-1: a run counts in the window that holds its end); the
HTTP targets keep MIN_REQUESTS. Under it
the network's success, challenge and block scores of that target are not_enough_data: the target
is left out of its browser_overall and challenge_overall (the weights renormalised over the
targets that count) and is no peer in that target's ranking, so a single browser run of a
network under the minimum moves neither its overall nor another network's rank.
"""

from __future__ import annotations

import copy
import math
from collections import defaultdict
from datetime import datetime

from .policy import (
    HTTP_DIFFICULTY,
    browser_difficulty,
    browser_min_requests,
    http_difficulty,
    success_score,
)
from .card_success import weights as card_weights

# A network is ranked for an HTTP target only with this many completed tries in the window (the
# standard error of a rate near 50% is 5 points at 100 tries, 7 at 50); for a browser target the
# methodology's browser_min_requests (SCORE-BROWSER-MIN-1).
MIN_REQUESTS = 100
# Fewer ranked networks score none: two would always be a 10 against a 1.
MIN_RANKED = 3
# A tight race: the spread between the best and worst ranked rate is under the larger of 3
# percentage points and 1.96 standard errors of that difference. Every ranked network then
# shares first place, a 10 (CHALLENGE-BLOCK-TIE-1, SUCCESS-TIE-1).
TIGHT_RACE_MIN_PTS = 3.0
TIGHT_RACE_Z = 1.96
# Each score: the outcome rate (browser.OUTCOMES) it ranks and whether higher is better.
METRICS = {"success": ("ok", True), "challenge": ("challenge", False), "block": ("block", False)}
# Each HTTP target score: the metrics rate it ranks and whether higher is better.
HTTP_METRICS = {
    "success": ("success_rate", True),
    "challenge": ("challenge_rate", False),
    "block": ("block_rate", False),
}
RANKED = "ranked"
# The Success x/10 of a network missing a card target (SUCCESS-ALL-8-1).
PARTIAL_TARGETS = "partial_targets"
NOT_ENOUGH_DATA = "not_enough_data"
TOO_FEW_NETWORKS = "too_few_networks"
# The overall's status when the network has no ranked target for the engine.
NO_RANKED_TARGETS = "no_ranked_targets"
# The metrics with an overall over every target: `<metric>_overall` (CHALLENGE-OVERALL-1).
OVERALL_METRICS = ("challenge",)
# The targets scored on success alone, from the HTTP metrics; their weight in challenge_overall
# is the methodology's [http_difficulty].
HTTP_TARGETS = tuple(HTTP_DIFFICULTY)


def score(products, difficulty, groups, http=HTTP_DIFFICULTY, minimum=None):
    """Score every window of build()'s product groups, label by label (see the module).
    difficulty: policy.browser_difficulty(); groups: the methodology's [target_groups];
    http: policy.http_difficulty(); minimum: policy.browser_min_requests(), else MIN_REQUESTS."""
    by_label = defaultdict(list)
    for p in products:
        for label, window in p["windows"].items():
            by_label[label].append(window)
    sources = {s for members in groups.values() for s in members}
    # The overall counts each target with a difficulty once: a group's sources count through it.
    counted = {t: d for t, d in difficulty.items() if t not in sources}
    for windows in by_label.values():
        _target_scores(windows, minimum)
        _overall(windows, counted)
        _http_scores(windows)
        for metric in OVERALL_METRICS:
            _metric_overall(windows, counted | http, metric)


def score_networks(networks, config):
    """score() of a build's network summaries (network.summaries()), each kind's networks
    ranked against each other per target, window and engine. config: the methodology."""
    kinds = defaultdict(list)
    for entry in networks:
        kinds[entry["kind"]].append(entry)
    difficulty, groups = browser_difficulty(config), config.get("target_groups", {})
    for peers in kinds.values():
        score(peers, difficulty, groups, http_difficulty(config), browser_min_requests(config))
        by_label = defaultdict(list)
        for entry in peers:
            for label, window in entry["windows"].items():
                by_label[label].append(window)
        settings, weights = success_score(config), card_weights(config)
        for windows in by_label.values():
            success_overall(windows, settings, weights)
            point_overall(windows, settings, weights)


def score_days(networks, config):
    """Each network's `days` scored (see the module): [{"start", "success_overall"}], the
    success_overall {score, rank, ranked_of, status, targets}."""
    peers = [{"kind": n["kind"], "windows": {d["start"]: d for d in n["days"]}} for n in networks]
    score_networks(peers, config)
    for entry in networks:
        days = []
        for day in entry["days"]:
            found = day.get("success_overall")
            found = {k: v for k, v in found.items() if k != "weights"} if found else None
            days.append({"start": day["start"], "success_overall": found})
        entry["days"] = days


def success_overall(windows, settings, weights):
    """Each window's `success_overall` from its "_success" (card_success.counts(), removed), the
    windows one label of one kind (see the module); settings: policy.success_score()'s {basis,
    slope, floor}; weights: card_success.weights()."""
    rates = defaultdict(dict)
    for i, window in enumerate(windows):
        for target, (completed, successes) in window.pop("_success", {}).items():
            if completed and target in weights:
                rates[target][i] = successes / completed
    best = {target: max(found.values()) for target, found in rates.items()}
    slope, floor = settings["slope"], settings["floor"]
    found = []
    for i, window in enumerate(windows):
        own = {
            t: max(floor, 10 - slope * 100 * (best[t] - rates[t][i]))
            for t in weights
            if i in rates.get(t, {})
        }
        used = {t: weights[t] for t in own}
        value = sum(own[t] * used[t] for t in own) / sum(used.values()) if own else None
        missing = [t for t in weights if t not in own]
        status = (PARTIAL_TARGETS if missing else RANKED) if own else NO_RANKED_TARGETS
        overall = {"score": value, "rank": None, "ranked_of": None, "status": status}
        overall |= {"targets": own, "weights": used, "missing_targets": missing}
        window["success_overall"] = overall | {"basis": settings["basis"]}
        if own:
            found.append(window["success_overall"])
    top = max((s["score"] for s in found), default=None)
    for s in found:  # the best network reads 10, the others keep their distance below it
        s["score"] = 10.0 if s["score"] == top else s["score"] + 10 - top
    _rank(found)


def point_overall(windows, settings, weights):
    """Each series point's `success_score` {score, status} of windows (one label of one kind):
    its "_success" (network.point_trailing(), removed) scored by success_overall() among the
    windows' points of the same time, so a point is the window's Success x/10 over the trailing
    window ending at it, its best network the kind's best over it (SUCCESS-POINTS-1). A point
    holding less than one whole day of the network's data ("_short") is null with
    not_enough_data and does not count for the best (SUCCESS-FULL-DAY-1). A window without a
    series (a day of score_days()) has none."""
    at, seen = defaultdict(list), set()
    for window in windows:
        for point in window.get("series", ()):
            if "_success" in point and id(point) not in seen:  # a point two windows share: once
                seen.add(id(point))
                at[point["at"]].append(point)
    for points in at.values():
        success_overall(points, settings, weights)
        for point in points:
            short = point.pop("_short", False)  # less than a day of data: empty, never 1.0
            if found := point.pop("success_overall", None):
                score = {"score": found["score"], "status": found["status"]}
                empty = {"score": None, "status": NOT_ENOUGH_DATA}
                point["success_score"] = empty if short else score


def _target_scores(windows, minimum):
    """Each engine's `scores` of every browser target in windows (one label), a network ranked
    with `minimum` completed tries, and each browser object's copy of its main engine's."""
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
            for own, s in zip(engines, metric_scores(rated, higher, minimum), strict=True):
                own["scores"][metric] = s
    for window in windows:
        for entry in window["targets"].values():
            if "browser" in entry:
                b = entry["browser"]
                b["scores"] = copy.deepcopy(b["engines"][b["engine"]]["scores"])


def metric_scores(rated, higher, minimum=None):
    """The score object of each (rate, completed tries) of the networks of one target, engine
    and window, in order: {"rate", "score", "rank", "ranked_of", "status"}; score, rank and
    ranked_of are null unless the status is ranked (with `minimum` tries, else MIN_REQUESTS). A
    tight race is every ranked network's shared first place (rank 1, a 10), as in the API's place
    and chart scores (api/fixtures/scores/success-score-cases.json)."""
    minimum = MIN_REQUESTS if minimum is None else minimum
    enough = [i for i, (r, n) in enumerate(rated) if n >= minimum and r is not None]
    ranked = [rated[i] for i in enough]
    status = TOO_FEW_NETWORKS if len(ranked) < MIN_RANKED else RANKED
    ranks = average_ranks([rate for rate, _ in ranked], higher) if status == RANKED else []
    if status == RANKED and similar(ranked):
        ranks = [1] * len(ranked)
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
    """Each HTTP target's `scores` in windows (one label) where it has metrics: its success,
    challenge and block rates (HTTP_METRICS) ranked as a browser engine's outcome rates."""
    for target in HTTP_TARGETS:
        found = [w["targets"][target] for w in windows if target in w["targets"]]
        found = [entry for entry in found if "completed" in entry["metrics"]]
        for entry in found:
            entry["scores"] = {}
        for metric, (key, higher) in HTTP_METRICS.items():
            rated = [(e["metrics"].get(key), e["metrics"]["completed"]) for e in found]
            for entry, s in zip(found, metric_scores(rated, higher), strict=True):
                entry["scores"][metric] = s


def _metric_overall(windows, weights, metric):
    """Each window's `<metric>_overall` (windows with a scored target only) over `weights`
    (target: weight): an HTTP target's own `metric` score, a browser target's main engine's;
    then the rank among the windows that have a score."""
    key = metric + "_overall"
    found = []
    for window in windows:
        scored = {}
        for target, entry in window["targets"].items():
            own = entry["browser"]["scores"] if "browser" in entry else entry.get("scores")
            if target in weights and own and metric in own:
                scored[target] = own[metric]
        if not scored:
            continue
        ranked = {t: s["score"] for t, s in scored.items() if s["status"] == RANKED}
        used = {t: weights[t] for t in ranked}
        value = sum(ranked[t] * used[t] for t in ranked) / sum(used.values()) if used else None
        status = RANKED if used else NO_RANKED_TARGETS
        overall = {"score": value, "rank": None, "ranked_of": None, "status": status}
        window[key] = overall | {"targets": ranked, "weights": used}
        if used:
            found.append(window[key])
    _rank(found)


def previous_scores(networks, previous, as_of):
    """Each network window's `previous_score` of success_overall and of each target's success
    score (see the module), from parts.previous()'s windows' `success` ({"overall", "basis",
    "targets": {id: score}}); null without a previous build, one newer than as_of, or that score,
    and the overall's null on another basis."""
    old = {}
    if previous and datetime.fromisoformat(previous["as_of"]) <= as_of:
        old = {n["id"]: n["windows"] for n in previous["networks"]}
    for entry in networks:
        for label, w in entry["windows"].items():
            found = old.get(entry["id"], {}).get(label, {}).get("success") or {}
            if own := w.get("success_overall"):  # never across two rules (SUCCESS-ALL-8-1)
                same = found.get("basis") == own.get("basis")
                own["previous_score"] = found.get("overall") if same else None
            before = found.get("targets") or {}
            for target, own in w["targets"].items():
                scores = own["browser"].get("scores") if "browser" in own else own.get("scores")
                if scores and "success" in scores:
                    scores["success"]["previous_score"] = before.get(target)
