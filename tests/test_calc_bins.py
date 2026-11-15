"""CALC-SCALE-1 (#1125): a bucket row's latency histogram kept as two integer arrays
(calculated/bins.py) reads as the list of (ms, count) pairs it replaces, and takes a fraction of
its memory.

That every build is unchanged is the incremental-equals-full tests' (test_calculated_incremental,
test_calc_rollups, test_calc_fleet): every bucket row they read now carries Bins.
"""

import json
import tracemalloc
from itertools import accumulate

from atlas.calculated.aggregate import iso
from atlas.calculated.bins import Bins, compact
from atlas.calculated.store import jsonable

PAIRS = [(31, 4), (35, 1), (48, 2), (3_000_000, 7)]


def test_bins_read_as_their_pairs():
    bins = Bins.of(PAIRS)
    assert list(bins) == PAIRS and len(bins) == 4
    assert dict(bins) == dict(PAIRS)
    assert bins == PAIRS and bins == [list(p) for p in PAIRS] and [list(p) for p in PAIRS] == bins
    assert bins == Bins(accumulate([31, 4, 13, 2_999_952]), [4, 1, 2, 7])
    assert bins != PAIRS[:3] and (bins == 5) is False
    assert repr(Bins.of([(1, 2)])) == "Bins([(1, 2)])"


def test_json_shows_the_pairs_so_the_fingerprint_keeps_its_shape():
    row = {"counters": {"latency_histogram": {"bins": Bins.of(PAIRS)}}}
    listed = {"counters": {"latency_histogram": {"bins": [list(p) for p in PAIRS]}}}
    assert jsonable(row) == listed
    assert json.dumps(row, default=iso) == json.dumps(listed)


def test_counters_are_compacted_in_place_and_large_values_fit():
    counters = {"completed": 3, "latency_histogram": {"bins": [[1, 2], [5, 2**40]]}}
    assert compact(counters) is counters
    assert isinstance(counters["latency_histogram"]["bins"], Bins)
    assert list(counters["latency_histogram"]["bins"]) == [(1, 2), (5, 2**40)]  # 64-bit
    assert compact({"completed": 0}) == {"completed": 0}  # no histogram: unchanged
    already = {"latency_histogram": {"bins": Bins.of(PAIRS)}}
    assert compact(already)["latency_histogram"]["bins"] is already["latency_histogram"]["bins"]


def test_a_histogram_takes_a_fraction_of_its_pairs_memory():
    """80 bins, as a production pulse bucket's histogram has on average: the pairs as tuples
    (rollups' former form) against Bins."""
    steps, counts = [40 + n for n in range(80)], [1 + n % 7 for n in range(80)]

    def traced(make):
        tracemalloc.start()
        made = [make() for _ in range(200)]
        size = tracemalloc.get_traced_memory()[0]
        tracemalloc.stop()
        assert len(made) == 200
        return size / 200

    pairs = traced(lambda: list(zip(accumulate(steps), counts, strict=True)))
    compacted = traced(lambda: Bins(accumulate(steps), counts))
    assert compacted < 1_000 and pairs > 4 * compacted, (pairs, compacted)
