"""CALC-SCALE-1 (#1125): the input fingerprint hashes the lists that grow with the window's runs
item by item (calculated/fingerprints.py), without a JSON copy of them all, and still changes
with any input.

That an unchanged hour reuses its build and a changed one does not is test_calc_geo_read's
(the rollup marks) and test_calculated_incremental's sweep.
"""

import tracemalloc
from datetime import UTC, datetime, timedelta

from atlas.calculated import fingerprints
from atlas.calculated.bins import Bins
from atlas.calculated.policy import digest
from atlas.calculated.store import jsonable

AT = datetime(2026, 11, 15, tzinfo=UTC)


def bucket(n):
    """A request bucket row as the calculation reads it, with an 80-bin histogram."""
    bins = Bins(range(40 + n % 5, 120 + n % 5), [1 + k % 7 for k in range(80)])
    counters = {"completed": 9, "successes": 8, "errors": {"timeout": 1}}
    counters |= {"latency_histogram": {"bins": bins}}
    at = AT + timedelta(minutes=5 * (n % 288))
    return {"bucket_start": at, "run_key": n, "counters": counters}


def inputs(n, **changed):
    found = {"runs": [{"run_key": k, "scope": {"config": {}}} for k in range(n)]}
    found |= {"buckets": [bucket(k) for k in range(n)], "release": {"id": 1}}
    found |= {"scheduled": {"pulses": [{"run_key": 1}], "geo": [[AT.isoformat(), 3]]}}
    return found | changed


def test_the_fingerprint_follows_every_input():
    base = fingerprints.of(inputs(20))
    assert fingerprints.of(inputs(20)) == base
    changes = [
        inputs(21),  # one more run and bucket
        inputs(20, release={"id": 2}),  # a value hashed whole
        inputs(20, scheduled={"pulses": [{"run_key": 2}], "geo": [[AT.isoformat(), 3]]}),
        inputs(20, scheduled={"pulses": [{"run_key": 1}], "geo": [[AT.isoformat(), 4]]}),
    ]
    swapped = inputs(20)
    swapped["runs"][0], swapped["runs"][1] = swapped["runs"][1], swapped["runs"][0]
    edited = inputs(20)
    edited["buckets"][7]["counters"]["latency_histogram"]["bins"] = Bins([1], [1])
    found = {fingerprints.of(i) for i in [*changes, swapped, edited]}
    assert base not in found and len(found) == 6
    lazy = inputs(20) | {"runs": map(dict, inputs(20)["runs"])}  # a map is hashed as its list
    assert fingerprints.of(lazy) == base


def test_items_of_mixed_keys_and_dicts_are_hashed():
    assert fingerprints.items([{1: "a", "b": 2}]) == fingerprints.items([{"1": "a", "b": 2}])
    assert fingerprints.items({"a": 1}) == fingerprints.items([("a", 1)])
    assert fingerprints.items([]).startswith("0:")


def test_hashing_holds_no_copy_of_the_rows():
    """2,000 bucket rows: the one-value digest held a JSON copy of them all; of() one row's."""
    rows = inputs(2_000)

    def peak(hash_them):
        tracemalloc.start()
        hash_them(rows)
        found = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        return found

    whole = peak(lambda value: digest(jsonable(value)))
    itemized = peak(fingerprints.of)
    assert itemized * 20 < whole, (itemized, whole)


def test_a_scope_is_hashed_once_and_still_changes_the_digest(monkeypatch):
    """CALC-SCALE-3 (#1307): a scope dict shared by its runs is hashed once, as production's
    are (incremental._interned()), and the digest still follows its content."""

    def shared(config):
        scope = {"config": config, "target": {"id": "generic_http"}}
        return {"runs": [{"run_key": k, "scope_id": 7, "scope": scope} for k in range(50)]}

    texts = []
    text = fingerprints._text
    monkeypatch.setattr(fingerprints, "_text", lambda value: texts.append(value) or text(value))
    base = fingerprints.of(shared({"concurrency": 4}))
    assert sum(1 for t in texts if isinstance(t, dict) and "config" in t) == 1
    assert fingerprints.of(shared({"concurrency": 4})) == base
    assert fingerprints.of(shared({"concurrency": 5})) != base
    own = {"runs": [r | {"scope": dict(r["scope"])} for r in shared({"concurrency": 4})["runs"]]}
    assert fingerprints.of(own) == base  # equal scopes, one dict each: the same digest
