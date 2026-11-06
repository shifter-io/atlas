"""Window latency objects: the percentiles and the mean (KEYS) of light-load latency, an
equal-weight mix of histograms (blend, blend_mean), and of latency under load (load_factor,
under_load_values, averaged; aggregate.under_load()).

Histograms are Counters of ms -> count, binned at the ceiling of each latency in ms (stats
Histogram), so a mean over them is up to about 1 ms above the mean of the raw latencies, as
the percentiles are.
"""

from __future__ import annotations

from collections import Counter

from ..stats import Histogram

LATENCY = ("p50", "p90", "p95", "p99")
FRACTIONS = {"p50": 0.5, "p90": 0.9, "p95": 0.95, "p99": 0.99}
# A window metrics latency object: the percentiles and the mean.
KEYS = (*LATENCY, "mean")


def blend(histograms):
    """Percentiles of an equal-weight mix of histograms (Counters of ms -> count).

    Each histogram is normalised to weight 1 and the weights are merged; the percentile is the
    nearest rank of the merged distribution (for one histogram, exactly Histogram.percentiles).
    Percentiles are never averaged.
    """
    result = dict.fromkeys(LATENCY)
    weights = Counter()
    used = 0
    for h in histograms:
        total = sum(h.values())
        if not total:
            continue
        used += 1
        for ms, n in h.items():
            weights[ms] += n / total
    if not used:
        return result
    ordered = sorted(weights.items())
    for label, fraction in FRACTIONS.items():
        rank, seen = fraction * used - 1e-9 * used, 0.0
        for ms, weight in ordered:
            seen += weight
            if seen >= rank:
                result[label] = ms
                break
        else:
            result[label] = ordered[-1][0]
    return result


def blend_mean(histograms):
    """Mean in ms of the mix blend() uses (each histogram weight 1), rounded to 0.1 ms as
    Histogram.mean (for one histogram, exactly it); None when no histogram has data."""
    means = [m for m in map(histogram_mean, histograms) if m is not None]
    return round(sum(means) / len(means), 1) if means else None


def blended(histograms):
    """blend() with its blend_mean() as "mean": a window metrics latency object."""
    histograms = list(histograms)
    return blend(histograms) | {"mean": blend_mean(histograms)}


def without_mean(latency):
    """A latency object's percentiles alone (series points carry no mean)."""
    return {p: latency[p] for p in LATENCY}


def histogram_mean(counts):
    """The unrounded mean in ms of a Counter of ms -> count; None when it has no data."""
    total = sum(counts.values())
    return sum(ms * n for ms, n in counts.items()) / total if total else None


def summary(counts):
    """A histogram's percentiles (Histogram.percentiles) and its unrounded mean, per KEYS."""
    return Histogram(counts).percentiles() | {"mean": histogram_mean(counts)}


def load_factor(load, pulse):
    """A load slot's factor (load_factors()): per KEYS, the load run's latency ÷ its reference
    pulse's (Counters of ms -> count); None unless every percentile of both is positive."""
    high, low = summary(load), summary(pulse)
    if not all(high[p] and low[p] for p in LATENCY):
        return None
    return {k: high[k] / low[k] for k in KEYS}


def under_load_values(histogram, factor):
    """One light-load slot's latency under load: its percentiles and mean (summary()) × the
    factor's, per KEYS; None where the factor has none (a factor without the mean)."""
    own = summary(histogram)
    return {k: own[k] * factor[k] if factor.get(k) else None for k in KEYS}


def averaged(values):
    """{key: the slots' values} -> each key's mean over the slots, rounded to 0.001 ms; None
    for a key without values or with a missing one."""
    return {
        k: round(sum(v) / len(v), 3) if v and None not in v else None for k, v in values.items()
    }
