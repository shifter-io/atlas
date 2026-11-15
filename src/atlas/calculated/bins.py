"""A latency histogram's bins kept as two integer arrays (CALC-SCALE-1, the build's memory).

Every request bucket row the calculation reads carries its latency histogram, about 80 bins. As a
list of (ms, count) pairs a pulse bucket row took ~8.5 KB in Python; with the bins in two arrays
~0.9 KB (measured on production rows), and the calculation holds one row per batch run and bucket
of the window. Bins iterates its (ms, count) pairs in their order, as the list did, so dict(bins)
and `for ms, n in bins` read it alike, compares equal to the same pairs as a list, and JSON (the
build's fingerprint: aggregate.iso()) shows it as the list of [ms, count] pairs.
"""

from __future__ import annotations

from array import array


def _array(values):
    """values as 32-bit integers, or 64-bit ones when one does not fit."""
    values = list(values)
    try:
        return array("i", values)
    except OverflowError:
        return array("q", values)


class Bins:
    """A histogram's (ms, count) bins in two arrays."""

    __slots__ = ("ms", "counts")

    def __init__(self, ms, counts):
        self.ms, self.counts = _array(ms), _array(counts)
        if len(self.ms) != len(self.counts):
            raise ValueError("a histogram needs one count per bin")

    @classmethod
    def of(cls, pairs):
        """The Bins of [(ms, count), ...] pairs, a histogram as JSON gives it."""
        pairs = list(pairs)
        return cls((ms for ms, _ in pairs), (n for _, n in pairs))

    def __iter__(self):
        return zip(self.ms, self.counts, strict=True)

    def __len__(self):
        return len(self.ms)

    def __eq__(self, other):
        try:
            return list(self) == [tuple(pair) for pair in other]
        except TypeError:
            return NotImplemented

    __hash__ = None

    def __repr__(self):
        return f"Bins({list(self)!r})"


def compact(counters):
    """counters with its latency histogram's bins as Bins (in place); returns counters."""
    histogram = counters.get("latency_histogram")
    if isinstance(histogram, dict) and isinstance(histogram.get("bins"), list):
        histogram["bins"] = Bins.of(histogram["bins"])
    return counters
