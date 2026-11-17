"""A calculation's input fingerprint, hashed without a second copy of its inputs (CALC-SCALE-1).

A build is reused when its inputs' fingerprint equals a stored build's. The inputs hold every run,
request bucket row and (in the full calculation) observation of the 30-day window; hashing them as
one JSON value (store.jsonable() then policy.digest()) held a JSON text and a parsed copy of all of
them at once, which grew with the window's runs. of() hashes those lists (LONG, and the scheduled
inputs' SCHEDULED) one item at a time instead, each as canonical JSON, and the rest as before. The
digests differ from the former ones, which only a build of the same implementation compares.

A run's scope (its configuration, about 4 KB of JSON) is shared by the scope's runs
(incremental._interned()), and hashing it with each run took about 0.15 ms a run, about 750 s an
hour at 30 days of production's runs (CALC-SCALE-3, #1307). Each scope is hashed once and each
run carries its scope's digest instead (_scoped()): the same inputs still give the same digest,
and a changed scope changes it.
"""

from __future__ import annotations

import hashlib
import json

from .aggregate import iso
from .policy import digest
from .store import jsonable

# The inputs that grow with the window's runs: one item per run, bucket row or observation, and
# the scheduled inputs' batch runs, tested spans and census overlaps.
LONG = ("runs", "buckets", "observations")
SCHEDULED = ("pulses", "tested", "overlaps")


def of(inputs):
    """The digest of inputs (a dict), its LONG lists and its "scheduled" dict's SCHEDULED ones
    digested item by item (items())."""
    flat = {
        k: items(_scoped(v) if k == "runs" else v) if k in LONG else v for k, v in inputs.items()
    }
    if "scheduled" in flat:
        flat["scheduled"] = {
            k: items(v) if k in SCHEDULED else v for k, v in flat["scheduled"].items()
        }
    return digest(jsonable(flat))


def items(values):
    """The count and digest of values' items in order, each as canonical JSON (sorted keys,
    datetimes and Bins as aggregate.iso() gives them; keys of mixed types as JSON returns them)."""
    found, n = hashlib.sha256(), 0
    for value in values.items() if isinstance(values, dict) else values:
        found.update(_text(value).encode() + b"\n")
        n += 1
    return f"{n}:{found.hexdigest()}"


def _scoped(runs):
    """The runs, each with its scope (a dict) as the digest of its canonical JSON, made once per
    scope dict (a scope's runs share one); a run without a scope dict is hashed as it is."""
    digests = {}
    for run in runs:
        scope = run.get("scope") if isinstance(run, dict) else None
        if isinstance(scope, dict):
            if id(scope) not in digests:  # the dict is kept, so its id is not reused
                text = _text(scope)
                digests[id(scope)] = (scope, hashlib.sha256(text.encode()).hexdigest())
            run = run | {"scope": digests[id(scope)][1]}
        yield run


def _text(value):
    """value as canonical JSON (items()' form)."""
    try:
        return _canonical(value, iso)
    except TypeError:
        return _canonical(jsonable(value), None)


def _canonical(value, default):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=default, allow_nan=False
    )
