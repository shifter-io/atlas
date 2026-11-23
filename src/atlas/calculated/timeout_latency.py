"""TIMEOUT-LATENCY-1 (the owner's decision of 2026-11-23, the same for every network): a request
that timed out counts in the latency as the time it waited, its run's timeout limit.

On the targets of the methodology's timeouts_in_latency (generic_http and generic_https), each
bucket row of a run gets, when the calculation reads it (counted()), one latency bin at the run's
timeout limit per request that timed out (its errors' "timeout" count): the run's own configured
timeout (its scope's timeout_seconds, in whole milliseconds), or the setting's limit_ms when it
names one. The row's successes stay as they are, so a time slot still votes only with
[slots] min_latency_successes successes. Every latency read from the rows follows, each by its
own rule: the headline over the country basket, every series point, the latency under load
(daily tests, census and load runs), the per-country latency block, the other targets'
display-only latency, the latency changes, and the mean beside the percentiles. Every other
failure (a refusal, a block, a connect, proxy or DNS error, a 404) ends before the limit and
stays out of the latency, as before. The per-place breakdowns' latency (exit address rows) keeps
its successes: a request that timed out has no exit address.

No stored row is edited: every bucket keeps its timeouts' count, so one recount applies the
rule to the whole stored window.
"""

from __future__ import annotations

import math
from collections import Counter

from .bins import Bins

SETTING = "timeouts_in_latency"
INVALID = "Invalid timeouts_in_latency"
TIMEOUT = "timeout"  # the error code of a request that timed out (transport._classify())


def rule(config) -> tuple[frozenset[str], int | None]:
    """The setting's targets (none without it) and its fixed limit_ms (None: each run's own);
    ValueError on an invalid one."""
    section = config.get(SETTING, {})
    if not isinstance(section, dict) or set(section) - {"targets", "limit_ms"}:
        raise ValueError(INVALID)
    found, limit = section.get("targets", []), section.get("limit_ms")
    if not isinstance(found, list) or len(set(found)) != len(found):
        raise ValueError(INVALID)
    if not all(isinstance(t, str) and t for t in found):
        raise ValueError(INVALID)
    if limit is not None and (type(limit) is not int or limit <= 0):
        raise ValueError(INVALID)
    return frozenset(found), limit


def limits(config, runs) -> dict:
    """{run key: timeout limit in ms} of the runs of the rule's targets."""
    wanted, fixed = rule(config)
    found = {}
    for r in runs:
        scope = r["scope"]
        if scope["target"]["id"] in wanted:
            seconds = scope["config"]["timeout_seconds"]
            found[r["run_key"]] = fixed or math.ceil(seconds * 1000)
    return found


def with_timeouts(counters: dict, limit: int) -> dict:
    """counters with one latency bin at limit (ms) per request that timed out."""
    n = int((counters.get("errors") or {}).get(TIMEOUT) or 0)
    if not n:
        return counters
    histogram = counters["latency_histogram"]
    bins = Counter(dict(histogram["bins"]))
    bins[limit] += n
    return counters | {"latency_histogram": histogram | {"bins": Bins.of(sorted(bins.items()))}}


def counted(config, runs, buckets):
    """The calculation's bucket rows, each of a run of the rule's targets with its timeouts in
    its latency histogram at the run's limit (with_timeouts())."""
    found = limits(config, runs)
    if not found:
        return buckets
    kept = []
    for b in buckets:
        if b["run_key"] in found and (b["counters"].get("errors") or {}).get(TIMEOUT):
            b = b | {"counters": with_timeouts(b["counters"], found[b["run_key"]])}
        kept.append(b)
    return kept
