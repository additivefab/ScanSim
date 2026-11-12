"""Replicate-level intervals and declared equivalence bands; never a device acceptance rule."""

import math

import numpy as np
from scipy.stats import t


def summarize(values, *, alpha=0.05):
    values = np.asarray([v for v in values if v is not None], float)
    if not np.all(np.isfinite(values)) or not 0 < alpha < 1:
        raise ValueError("replicate values and confidence level must be finite and valid")
    if not len(values):
        return {"n": 0, "mean": None, "ci": None}
    mean = float(values.mean())
    if len(values) == 1:
        return {"n": 1, "mean": mean, "ci": None}
    half = float(t.ppf(1 - alpha / 2, len(values) - 1) * values.std(ddof=1) / np.sqrt(len(values)))
    return {"n": len(values), "mean": mean, "ci": [mean - half, mean + half]}


def equivalence(
    left, right, margin, *, alpha, paired=False, log_ratio=False, bounded_fraction=False
):
    """Welch difference or paired-replicate interval, with explicitly declared margin/alpha."""
    if not np.isfinite(margin) or margin <= 0 or not 0 < alpha < 1:
        raise ValueError("equivalence margin and confidence level must be valid")
    pairs = [(a, b) for a, b in zip(left, right, strict=True)] if paired else None
    if paired:
        pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
        left, right = zip(*pairs, strict=True) if pairs else ([], [])
    else:
        left = [v for v in left if v is not None]
        right = [v for v in right if v is not None]
    a, b = np.asarray(left, float), np.asarray(right, float)
    if not np.all(np.isfinite(a)) or not np.all(np.isfinite(b)):
        raise ValueError("replicates must be finite")
    result = {
        "n_left": len(a),
        "n_right": len(b),
        "paired": paired,
        "margin": margin,
        "alpha": alpha,
        "log_ratio": log_ratio,
        "estimate": None,
        "ci": None,
        "status": "insufficient_replicates",
    }
    if min(len(a), len(b)) < 2:
        return result
    if bounded_fraction:
        if log_ratio or np.any((a < 0) | (a > 1)) or np.any((b < 0) | (b > 1)):
            raise ValueError("fraction replicates must lie in [0, 1]")
        estimate = float(a.mean() - b.mean())
        if paired:
            half = 2 * math.sqrt(math.log(2 / alpha) / (2 * len(a)))
        else:
            half = math.sqrt(math.log(4 / alpha) / (2 * len(a))) + math.sqrt(
                math.log(4 / alpha) / (2 * len(b))
            )
        ci = [max(-1.0, estimate - half), min(1.0, estimate + half)]
        result.update(
            estimate=estimate,
            ci=ci,
            method="bounded Hoeffding; independent runs",
            status="equivalent"
            if -margin < ci[0] and ci[1] < margin
            else "outside_equivalence_band"
            if ci[0] > margin or ci[1] < -margin
            else "inconclusive",
        )
        return result
    if log_ratio:
        if np.any(a <= 0) or np.any(b <= 0):
            raise ValueError("ratio intervals require positive replicate values")
        a, b = np.log(a), np.log(b)
    estimate = float(a.mean() - b.mean())
    if paired:
        delta = a - b
        variance = float(delta.var(ddof=1) / len(delta))
        degrees = len(delta) - 1
    else:
        va, vb = a.var(ddof=1) / len(a), b.var(ddof=1) / len(b)
        variance = float(va + vb)
        denominator = va**2 / (len(a) - 1) + vb**2 / (len(b) - 1)
        degrees = (va + vb) ** 2 / denominator if denominator else math.inf
    half = float(t.ppf(1 - alpha / 2, degrees) * np.sqrt(variance)) if variance else 0.0
    ci = [estimate - half, estimate + half]
    result.update(
        estimate=estimate,
        ci=ci,
        status="equivalent"
        if -margin < ci[0] and ci[1] < margin
        else "outside_equivalence_band"
        if ci[0] > margin or ci[1] < -margin
        else "inconclusive",
    )
    if log_ratio:
        result["ratio_estimate"] = math.exp(estimate)
        result["ratio_ci"] = [math.exp(v) for v in ci]
    return result


def summarize_fraction(values, *, alpha=0.05):
    values = np.asarray([v for v in values if v is not None], float)
    if not np.all(np.isfinite(values)) or np.any((values < 0) | (values > 1)) or not 0 < alpha < 1:
        raise ValueError("fraction replicates and confidence level must be valid")
    if not len(values):
        return {"n": 0, "mean": None, "ci": None, "method": "bounded Hoeffding"}
    mean = float(values.mean())
    half = math.sqrt(math.log(2 / alpha) / (2 * len(values)))
    return {
        "n": len(values),
        "mean": mean,
        "ci": [max(0.0, mean - half), min(1.0, mean + half)],
        "method": "bounded Hoeffding",
    }
