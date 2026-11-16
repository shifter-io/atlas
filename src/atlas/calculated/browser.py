"""Browser targets in the calculated snapshot (browser pulse K-BP6 and D9; docs/browser.html).

Browser runs (batch kind browser) are measured per target only. split() keeps their bucket rows
out of every network-level figure, so a network's window metrics, census overlap, series, uptime,
coverage, scores, fleet totals and breakdowns are exactly what they would be without any browser
run. targets() hands the calculator's per-target loop each browser target's rows after the
network targets, with the K-BP6 browser object: outcome rates, page-load times, group and
sources. A group target ([target_groups] of the methodology: social_web and ai_answers) is
measured over the union of its sources' rows and is never scheduled itself.

Engines are never added together. Each row carries its run's engine (K-BP3; a run that names
none is Camoufox). A target's own figures in a window (its window metrics, outcome rates and
page loads) are those of one engine, the one with the most completed tries (ties go to
Camoufox), which the browser object names in `engine`; `engines` holds every engine's own.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from ..browser.contracts import BROWSER_ENGINE, BROWSER_KIND, BROWSER_TARGETS
from ..browser.engines import DEFAULT_ENGINE
from ..stats import Histogram
from ..targets import registry

# The role label of a browser run (calculator.BATCH_ROLES).
ROLES = {BROWSER_KIND: "browser"}
# SQL condition: the run (b: its test_batches row, if it has one) is not a browser run.
NOT_BROWSER = f"(b.kind IS NULL OR b.kind<>'{BROWSER_KIND}') "
# K-BP6 outcome_rates, in order. challenge, block, timeout and proxy_error come from the typed
# bucket counters (their verdicts), login_wall, no_content and no_answer from the error codes of
# the error verdict (K-BP2); other_error is every other failed try.
OUTCOMES = (
    "ok",
    "challenge",
    "block",
    "login_wall",
    "timeout",
    "proxy_error",
    "no_content",
    "no_answer",
    "other_error",
)
COUNTERS = {
    "challenge": "challenges",
    "block": "blocks",
    "timeout": "timeouts",
    "proxy_error": "proxy_errors",
}
CODES = ("login_wall", "no_content", "no_answer")


def engine(target):
    """A target's engine: the registry's; a K-BP1 browser target not registered yet is browser,
    any other unknown target http."""
    try:
        return registry.get(target).engine
    except ValueError:
        return BROWSER_ENGINE if target in BROWSER_TARGETS else "http"


def split(rows):
    """(network rows, browser rows) of a group's bucket rows, each in its original order. A row
    is a browser row when its run's batch kind (test) is browser or its target's engine is (a
    pulse batch of a browser target is browser too)."""
    network, browser = [], []
    for row in rows:
        is_browser = row.get("test") == BROWSER_KIND or engine(row["target"]) == BROWSER_ENGINE
        (browser if is_browser else network).append(row)
    return network, browser


def targets(config, rows, browser_rows, since, end):
    """(target, rows, extra) of every target of a window, in order: the network targets (the
    required ones and every measured one, by id) with no extra, then each browser target of
    browser_targets() with {"browser": fields()}. rows are the window's network rows,
    browser_rows every browser row of the group."""
    ids = set(config["required_targets"]) | {r["target"] for r in rows}
    found = [(t, [r for r in rows if r["target"] == t], {}) for t in sorted(ids)]
    groups = config.get("target_groups", {})
    for target, own in browser_targets(browser_rows, since, end, groups).items():
        extra = {"browser": fields(target, own, groups)}
        found.append((target, by_engine(own)[extra["browser"]["engine"]], extra))
    return found


def run_engine(config):
    """The engine of a run, from its scope's config (K-BP3 `browser_engine`); Camoufox when it
    names none, as every run before the engine choice."""
    return config.get("browser_engine") or DEFAULT_ENGINE


def by_engine(rows):
    """A target's rows per engine (each row's `engine`, Camoufox when it has none), by engine
    name, each in its original order."""
    found = defaultdict(list)
    for row in rows:
        found[row.get("engine") or DEFAULT_ENGINE].append(row)
    return {name: found[name] for name in sorted(found)}


def lead_engine(engines):
    """The engine whose rows make a target's own figures: the most completed tries, ties going
    to Camoufox (then the first by name). engines: engine_fields() by engine name."""
    best = max(engines, key=lambda e: (engines[e]["completed"], e == DEFAULT_ENGINE), default=None)
    return best or DEFAULT_ENGINE


def browser_targets(rows, since, end, groups):
    """The browser rows of [since, end) per browser target, plus each group target with any
    source rows (group_target()), in the K-BP1 order (others after them, by id). A target with
    no rows in the window has no entry, so a window without browser runs has none."""
    window = [r for r in rows if since <= r["bucket_start"] < end]
    found = defaultdict(list)
    for row in window:
        found[row["target"]].append(row)
    for group, sources in groups.items():
        if union := group_target(window, sources):
            found[group] = union
    order = {t: i for i, t in enumerate(BROWSER_TARGETS)}
    return {t: found[t] for t in sorted(found, key=lambda t: (order.get(t, len(order)), t))}


def group_target(rows, sources):
    """A group target's rows: the union of its sources' rows, in their original order."""
    wanted = set(sources)
    return [r for r in rows if r["target"] in wanted]


def fields(target, rows, groups):
    """The K-BP6 browser object of a browser target's bucket rows: outcome_rates and
    page_load_ms of its lead engine (lead_engine()), group (the group it is a source of, or
    null), sources (a group's, in methodology order, or null), engine (the lead engine) and
    engines (engine_fields() of each engine with rows, by name)."""
    engines = {name: engine_fields(own) for name, own in by_engine(rows).items()}
    lead = lead_engine(engines)
    own = engines.get(lead) or engine_fields([])
    return {
        "outcome_rates": own["outcome_rates"],
        "page_load_ms": own["page_load_ms"],
        "group": next((g for g, sources in groups.items() if target in sources), None),
        "sources": list(groups[target]) if target in groups else None,
        "engine": lead,
        "engines": engines,
    }


def engine_fields(rows):
    """One engine's figures of a target: completed and successful tries, outcome_rates and
    page_load_ms."""
    return {
        "completed": sum(r["counters"]["completed"] for r in rows),
        "successes": sum(r["counters"]["successes"] for r in rows),
        "outcome_rates": outcome_rates(rows),
        "page_load_ms": page_load(rows),
    }


def outcome_rates(rows):
    """Each OUTCOMES share of the completed tries; every rate is null when none completed."""
    counts = Counter()
    completed = 0
    for row in rows:
        c = row["counters"]
        completed += c["completed"]
        counts["ok"] += c["successes"]
        for name, key in COUNTERS.items():
            counts[name] += c.get(key, 0)
        for code in CODES:
            counts[code] += c["errors"].get(code, 0)
    counts["other_error"] = completed - sum(counts.values())
    return {k: counts[k] / completed if completed else None for k in OUTCOMES}


def page_load(rows):
    """Page-load percentiles (ms: p50, p90, p95, p99) of the successful tries, whatever the run:
    a bucket's histogram holds its successes only."""
    histogram = Histogram()
    for row in rows:
        bins = row["counters"]["latency_histogram"]["bins"]
        histogram.merge(Histogram(Counter(dict(bins))))
    return histogram.percentiles()
