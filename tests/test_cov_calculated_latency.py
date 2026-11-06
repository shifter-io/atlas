"""calculated/latency.py: a histogram without requests has no weight in a blend."""

from collections import Counter

from proxy_index.calculated.latency import LATENCY, blend, blended


def test_an_empty_histogram_neither_votes_nor_dilutes_the_blend():
    fast, slow = Counter({100: 3, 200: 1}), Counter({900: 2})
    assert blend([Counter(), fast, Counter({500: 0}), slow]) == blend([fast, slow])
    # Two equal-weight slots: the median is the fast slot's top bin, the tail the slow slot.
    assert blend([fast, Counter(), slow]) == {"p50": 200, "p90": 900, "p95": 900, "p99": 900}


def test_a_blend_of_empty_histograms_has_no_latency():
    assert blend([Counter(), Counter({100: 0})]) == dict.fromkeys(LATENCY)
    assert blended(iter([Counter()])) == dict.fromkeys(LATENCY) | {"mean": None}
