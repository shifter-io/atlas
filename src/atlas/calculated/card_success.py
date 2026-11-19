"""The Success x/10's inputs on the card targets (SUCCESS-ALL-8-1, the owner's decision of
2026-11-19; methodology [success_score] basis "card_targets", target_scores.success_overall()).

The card targets are value_all.card_targets(): the HTTP targets, then each browser target or
group that is not a group's source (8 today), each weighing the same (weights(): 1 each, 12.5%;
the owner's correction of 2026-11-19, not their difficulty).
Their tries are the ones the all-targets bill prices (value_all.py) and successRateAllTargets
reads: an HTTP target's every request in the window (pulses, daily tests, census and load runs:
its window metrics' completed and successes); a browser target's runs counted in its browser
window (browser.counted_at(): a whole run, by its end), of its lead engine only
(browser.lead_engine(), the engine of its window metrics), a group's over the union of its
sources' rows. A browser target's window is the window's own, or the [value] browser_window
(7d) when that is longer (browser_since()): each browser target runs once a UTC day at a
shifting hour, so 24 hours often miss one, and the bill prices a 24h window's browser targets
over the 7 days too (value_all.reprice()). A target without a completed try has no count; one
with tries and no success counts, at 0%.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import defaultdict
from datetime import timedelta

from .. import verdicts
from ..browser.engines import DEFAULT_ENGINE
from . import browser
from .policy import http_difficulty, value_browser_window
from .value_all import HOURS, card_targets


def weights(config):
    """Each card target's weight in the Success x/10, in card order: 1, equal weights."""
    return dict.fromkeys(card_targets(config), 1)


def browser_since(config, end, hours):
    """The start of the browser window of a window of `hours` ending at end: `hours` before
    end, or the [value] browser_window's when that is longer."""
    wide = HOURS.get(value_browser_window(config), 0)
    return end - timedelta(hours=max(hours, wide))


def counts(config, http, browser_rows, since, end):
    """{target: [completed, successes]} of a window's card targets with a completed try, in card
    order: http, the HTTP targets' (http_counts(), Trailing.counts()), and browser_counts() of
    browser_rows counted in [since, end), the browser window."""
    found = http | browser_counts(config, browser_rows, since, end)
    return {t: found[t] for t in card_targets(config) if t in found and found[t][0] > 0}


def http_counts(config, rows):
    """{target: [completed, successes]} of the HTTP targets among a window's network rows."""
    found, targets = {}, http_difficulty(config)
    for r in rows:
        if r["target"] in targets:
            own = found.setdefault(r["target"], [0, 0])
            own[0] += r["counters"]["completed"]
            own[1] += r["counters"]["successes"]
    return found


def browser_counts(config, rows, since, end):
    """{target: [completed, successes]} of each browser card target (a group: its sources' rows)
    among browser rows counted in [since, end), its lead engine's."""
    group_of = {s: g for g, sources in config.get("target_groups", {}).items() for s in sources}
    cards = set(card_targets(config)) - set(http_difficulty(config))
    engines = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for r in rows:
        target = group_of.get(r["target"], r["target"])
        if target in cards and since <= browser.counted_at(config, r) < end:
            own = engines[target][r.get("engine") or DEFAULT_ENGINE]
            own[0] += r["counters"]["completed"]
            own[1] += r["counters"]["successes"]
    found = {}
    for target, own in engines.items():
        lead = browser.lead_engine({e: {"completed": own[e][0]} for e in sorted(own)}, target)
        found[target] = own[lead]
    return found


class Trailing:
    """http_counts() of any span of a network's network rows, from prefix sums (a chart point's
    trailing window, network.point_trailing()): the rows aggregate.in_window() keeps, the 24h
    window's with the daily tests it reaches back to."""

    def __init__(self, config, rows):
        targets = http_difficulty(config)
        own = sorted((r for r in rows if r["target"] in targets), key=lambda r: r["bucket_start"])
        self.first = own[0]["bucket_start"] if own else None
        self.reach = [r for r in own if r.get("reach")]
        self.keys, self.sums = {}, {}
        for t in targets:
            mine = [r for r in own if r["target"] == t]
            self.keys[t] = [r["bucket_start"] for r in mine]
            self.sums[t] = total = [(0, 0)]
            for r in mine:
                c = r["counters"]
                total.append((total[-1][0] + c["completed"], total[-1][1] + c["successes"]))

    def counts(self, since, end, hours):
        """http_counts() of in_window(rows, since, end, hours)."""
        found = {}
        for t, keys in self.keys.items():
            low, high = self.sums[t][bisect_left(keys, since)], self.sums[t][bisect_left(keys, end)]
            if high[0] > low[0]:
                found[t] = [high[0] - low[0], high[1] - low[1]]
        for r in self.reach if hours == verdicts.REACH_WINDOW else ():
            if r["bucket_start"] < since:
                own = found.setdefault(r["target"], [0, 0])
                own[0] += r["counters"]["completed"]
                own[1] += r["counters"]["successes"]
        return found
