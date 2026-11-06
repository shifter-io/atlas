"""Latency of an equal-weight mix of histograms: percentiles (blend) and the mean (blend_mean).

Histograms are Counters of ms -> count, binned at the ceiling of each latency in ms (stats
Histogram), so a mean over them is up to about 1 ms above the mean of the raw latencies, as
the percentiles are.
"""

from __future__ import annotations

from collections import Counter

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
    totals = [(h, sum(h.values())) for h in histograms]
    means = [sum(ms * n for ms, n in h.items()) / total for h, total in totals if total]
    return round(sum(means) / len(means), 1) if means else None


def blended(histograms):
    """blend() with its blend_mean() as "mean": a window metrics latency object."""
    histograms = list(histograms)
    return blend(histograms) | {"mean": blend_mean(histograms)}
