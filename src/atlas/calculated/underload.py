"""Time slots and the two latencies: the slots of a window's rows (time_slots), a slot's success
vote (slot_rate), the votes of its slots and targets (light_votes: normal requests, the pulses;
or the requests under load: the latency target's daily tests, census and load runs), the latency
under load they make (under_load, with_under_load) and the window's latency block
(latency_detail).

A window's figures, its days, series points, targets and latency blocks call time_slots() many
times over the same rows (CALC-PERF-2: about a tenth of a build's time, and growing with the
window). Inside slot_memo() (one network's summary, network.summary()) each time slot's rows
are counted once: a slot whose rows are the same row objects as before, in the same
order, reuses that slot's counts (each caller gets its own copy of the slot's dict; the
histograms are shared, and nothing writes them). Rows the build holds (inputs.BucketRow) are
counted once; a plain dict row, a copy made for one count (aggregate.census_split()), each time.
The slots keep the order in which their keys first appear in the rows, so every figure is what
counting them again gives.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import contextmanager

from .latency import blended
from .scores import UNDER_LOAD_NONE

UNDER_LOAD_LIGHT = "No slot under load with enough successes in this window"
UNDER_LOAD_CENSUS = "Every load test in this window ran during a census"


def time_slots(rows, pulses=()):
    """Bucket rows (each with its time "slot") grouped per slot.

    A slot holds every request observed in it: "completed"/"successes" count the requests that
    vote on its success, "all_completed"/"all_successes" every request, including pulses and
    load waves that ran during a census of the product (rows with "during_census"). Per target
    it merges the histogram of its light-load successes, the normal requests ("light": {target:
    Counter}, "light_successes": {target: n}; rows without the "light" flag or with the "loaded"
    one stay out), and of its successes under load ("loaded", "loaded_successes": rows with the
    "loaded" flag, the latency target's daily tests, census and load runs). A row's histogram
    holds its timeouts too, at their run's limit (timeout_latency.py); the *_successes count its
    successes only, so a slot votes by its successes. pulses are the product's done pulse runs
    ({"slot", "requests", "successes", "counted", "during_census"}): the live counters of a run
    whose artifact was never ingested (counted False) are added to its slot as observed; nothing
    is invented for requests nobody observed.
    """
    memo = _MEMOS[-1] if _MEMOS else None
    slots = _counted(rows) if memo is None else _memoized(rows, memo)
    for d in pulses:
        if not d["counted"]:
            s = slots.setdefault(d["slot"], _slot())
            _add(s, d["requests"], d["successes"], not d.get("during_census"))
    return slots


_MEMOS = []  # the open slot_memo()s' memos, the innermost last


@contextmanager
def slot_memo():
    """time_slots() counts each slot's rows once inside it (module docstring)."""
    _MEMOS.append({})
    try:
        yield
    finally:
        _MEMOS.pop()


def _counted(rows):
    slots = {}
    for r in rows:
        key = r["slot"]
        if key not in slots:
            slots[key] = _slot()
        _row(slots[key], r)
    return slots


def _memoized(rows, memo):
    """_counted(rows), each slot's counts taken from memo when its rows were counted before:
    memo maps (slot, the ids of its rows) to its counts and its rows, which it keeps alive, so no
    other row takes their ids."""
    own = {}
    for r in rows:
        own.setdefault(r["slot"], []).append(r)
    slots = {}
    for key, found in own.items():
        if any(isinstance(r, dict) for r in found):  # a copy made for one count
            slots[key] = _counted(found)[key]
            continue
        ids = (key, *map(id, found))
        if ids not in memo:
            memo[ids] = (_counted(found)[key], found)
        slots[key] = dict(memo[ids][0])
    return slots


def _slot():
    return {
        "completed": 0,
        "successes": 0,
        "all_completed": 0,
        "all_successes": 0,
        "light": defaultdict(Counter),
        "light_successes": Counter(),
        "loaded": defaultdict(Counter),
        "loaded_successes": Counter(),
    }


def _add(s, completed, successes, clean):
    s["all_completed"] += completed
    s["all_successes"] += successes
    if clean:
        s["completed"] += completed
        s["successes"] += successes


def _row(s, r):
    """Add bucket row r to its slot's counts s."""
    c = r["counters"]
    _add(s, c["completed"], c["successes"], not r.get("during_census"))
    kind = "loaded" if r.get("loaded") else "light" if r.get("light", True) else None
    if kind:
        target = r.get("target")
        for ms, n in c["latency_histogram"]["bins"]:
            s[kind][target][ms] += n
        s[f"{kind}_successes"][target] += c["successes"]


def slot_rate(s):
    """A slot's success vote: successes ÷ its voting requests (every request except pulses and
    load waves that ran during a census). A slot left with only such requests votes 0 when all
    of them failed too (the provider was down) and does not vote otherwise. None: no vote."""
    if s["completed"]:
        return s["successes"] / s["completed"]
    if s["all_completed"] and not s["all_successes"]:
        return 0.0
    return None


def light_votes(slots, minimum, kind="light"):
    """(slot, target, histogram) of every slot and target with `minimum` successes of the kind
    (time_slots(): "light", normal requests, or "loaded", under load): each votes with equal
    weight, so a target or a slot never outweighs another by volume."""
    return [
        (key, target, s[kind][target])
        for key, s in sorted(slots.items())
        for target in sorted(s[kind], key=str)
        if s[f"{kind}_successes"][target] >= minimum
    ]


def under_load(rows, minimum):
    """Latency under load: the requests of the latency target at load concurrency (rows flagged
    "loaded": its daily tests, census and load runs that did not run during a census), measured
    as light-load latency is (light_votes(): each slot and target with `minimum` successes
    weighs the same). slots counts the voting slots; reason says why there is no value."""
    votes = light_votes(time_slots(rows), minimum, "loaded")
    reason = None
    if not votes:
        reason = (
            UNDER_LOAD_LIGHT
            if any(r.get("loaded") for r in rows)
            else UNDER_LOAD_CENSUS
            if any(r.get("test") == "load" and r.get("during_census") for r in rows)
            else UNDER_LOAD_NONE
        )
    return {
        "ms": blended(h for *_, h in votes),
        "slots": len({key for key, *_ in votes}),
        "reason": reason,
    }


def with_under_load(m, rows, minimum):
    """Add latency_under_load_ms and its slot count and reason to timed() metrics."""
    result = under_load(rows, minimum)
    m["latency_under_load_ms"] = result.pop("ms")
    m["under_load"] = result
    return m


def load_references(rows):
    """Per runner region, the country, target and concurrency of its newest load run outside a
    census (the load wave's reference country)."""
    references = {}
    for r in sorted(rows, key=lambda r: (r["slot"], r["target"])):
        if r.get("test") == "load" and not r.get("during_census"):
            references[r.get("region") or ""] = {
                "country": r["country"],
                "target": r["target"],
                "concurrency": r.get("concurrency"),
            }
    return references


def latency_detail(rows, minimum, choices=()):
    """The window's latency block (rows: the latency target's). regions: per runner region its
    load wave's reference country (load_references()) and the daily choices that named it
    (choices: load_references rows {"day", "region", "country", "basis", "p50_ms", "successes",
    "candidates"}). countries: per requested country (and the region its pulses ran from) the
    latency of its normal requests (pulses) and under load (daily tests, census and load runs)."""
    own = defaultdict(list)
    for r in rows:
        if r.get("test") == "pulse" or r.get("loaded"):
            own[r["country"] or ""].append(r)
    countries = []
    for country, found in sorted(own.items()):
        slots = time_slots(found)
        result = under_load(found, minimum)
        countries.append(
            {
                "country": country or None,
                "region": min((r["region"] for r in found if r.get("region")), default=None),
                "latency_ms": blended(h for *_, h in light_votes(slots, minimum)),
                "latency_under_load_ms": result.pop("ms"),
                **result,
            }
        )
    references = load_references(rows)
    chosen = defaultdict(list)
    for c in sorted(choices, key=lambda c: (c["region"], c["day"])):
        chosen[c["region"]].append({k: c[k] for k in c if k != "region"})
    regions = [
        {
            "region": region or None,
            "reference": references.get(region),
            "chosen": chosen.get(region, []),
        }
        for region in sorted(set(references) | set(chosen))
    ]
    return {
        "regions": regions,
        "basis": "normal requests = pulses; under load = the latency target's daily tests, "
        "census and load runs; each slot with enough successes weighs the same",
        "countries": countries,
    }
