"""Merge reported latency counts; never average percentile values."""
import math


def validate_histogram(bins):
    if not isinstance(bins, list) or not bins or len(bins) > 100000:
        raise ValueError("Missing or oversized histogram")
    previous = -1
    for row in bins:
        if (not isinstance(row, list) or len(row) != 2 or
                any(not isinstance(value, int) or isinstance(value, bool) for value in row)
                or row[0] <= previous or row[1] <= 0):
            raise ValueError("Histogram requires sorted unique integer values and positive counts")
        previous = row[0]
    return bins


def merge(histograms):
    counts = {}
    for histogram in histograms:
        for value, count in validate_histogram(histogram):
            counts[value] = counts.get(value, 0) + count
    if not counts:
        raise ValueError("No histogram samples")
    return [[value, count] for value, count in sorted(counts.items())]


def percentile(bins, p):
    bins = validate_histogram(bins)
    if not isinstance(p, (int, float)) or not math.isfinite(p) or not 0 < p <= 100:
        raise ValueError("Invalid percentile")
    total = sum(count for _, count in bins)
    # Match wrk's round(N*p/100 + 0.5) rank for positive values.
    rank = min(total, math.floor(total * p / 100 + 1))
    cumulative = 0
    for value, count in bins:
        cumulative += count
        if cumulative >= rank:
            return value
    raise ValueError("Histogram count inconsistency")


def summarize(histograms):
    bins = merge(histograms)
    return {"sample_count": sum(count for _, count in bins), "bins_us": bins,
            "p50_ms": percentile(bins, 50) / 1000, "p95_ms": percentile(bins, 95) / 1000,
            "p99_ms": percentile(bins, 99) / 1000,
            "semantics": "merged wrk-reported distributions; count may differ from completed requests"}
