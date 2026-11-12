"""Global static response from observed reference statistics and declared uncertainty bounds."""

from dataclasses import dataclass

import numpy as np


class UnidentifiableTransfer(ValueError):
    def __init__(self, reason: str, diagnostics=None):
        super().__init__(f"unidentifiable observed transfer: {reason}")
        self.diagnostics = diagnostics or {}


@dataclass(frozen=True)
class MonotonicTransferFit:
    code_knots: tuple[float, ...]
    intensity_knots: tuple[float, ...]
    valid_code_ranges: tuple[tuple[float, float], ...]
    excluded_code_ranges: tuple[tuple[float, float], ...]
    reference_groups: tuple[dict, ...]
    unidentifiable_edges: tuple[dict, ...]
    supported_edges: tuple[dict, ...]
    normalization: dict
    interpolation: str
    uncertainty_scope: str


def _merge_ranges(ranges):
    merged = []
    for low, high in sorted(ranges):
        if merged and low <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(high, merged[-1][1]))
        else:
            merged.append((low, high))
    return tuple(merged)


def fit_monotonic_transfer(
    codes, relative_signal, *, code_half_width, signal_bounds, lut_points: int
) -> MonotonicTransferFit:
    """No forced monotonization or extrapolation; uncertainty is required, not guessed."""
    if code_half_width is None or signal_bounds is None:
        raise UnidentifiableTransfer("reference uncertainty has not been declared")
    code, signal, half, bounds = (
        np.asarray(v, float) for v in (codes, relative_signal, code_half_width, signal_bounds)
    )
    if (
        code.ndim != 1
        or len(code) < 3
        or signal.shape != code.shape
        or half.shape != code.shape
        or bounds.shape != (len(code), 2)
        or not all(np.all(np.isfinite(v)) for v in (code, signal, half, bounds))
        or np.any(half < 0)
        or np.any(signal < 0)
        or np.any(signal > 1)
        or np.any(bounds[:, 0] < 0)
        or np.any(bounds[:, 0] > signal)
        or np.any(bounds[:, 1] < signal)
        or type(lut_points) is not int
        or not 2 <= lut_points <= 100_000
    ):
        raise ValueError("invalid observed reference values, uncertainty bounds or LUT resolution")
    if not np.any(signal == 0) or not np.any(signal == 1):
        raise UnidentifiableTransfer("relative dark=0 and white=1 references are required")
    groups = []
    for value in np.unique(signal):
        indices = np.flatnonzero(signal == value)
        lows, highs = code[indices] - half[indices], code[indices] + half[indices]
        if lows.max() > highs.min():
            raise UnidentifiableTransfer(
                "duplicate signal references have disjoint code bounds",
                {"source_indices": indices.tolist()},
            )
        groups.append(
            {
                "signal": float(value),
                "code": float(np.median(code[indices])),
                "code_bounds": (float(lows.min()), float(highs.max())),
                "signal_bounds": (float(bounds[indices, 0].min()), float(bounds[indices, 1].max())),
                "source_indices": tuple(int(i) for i in indices),
            }
        )
    polarity = np.sign(groups[-1]["code"] - groups[0]["code"])
    if not polarity or np.any(np.diff([polarity * g["code"] for g in groups]) < 0):
        raise UnidentifiableTransfer(
            "reference centres are not monotonic; no silent correction", {"groups": groups}
        )
    valid, excluded, bad_edges, good_edges = [], [], [], []
    for left, right in zip(groups[:-1], groups[1:], strict=True):
        q_left = sorted(polarity * np.asarray(left["code_bounds"]))
        q_right = sorted(polarity * np.asarray(right["code_bounds"]))
        extent = (
            min(left["code_bounds"][0], right["code_bounds"][0]),
            max(left["code_bounds"][1], right["code_bounds"][1]),
        )
        reason = None
        if q_left[1] >= q_right[0]:
            reason = "overlapping code uncertainty"
        elif left["signal_bounds"][1] >= right["signal_bounds"][0]:
            reason = "overlapping signal uncertainty"
        elif left["signal"] == 0:
            bad_edges.append(
                {
                    "reason": "dark is normalization only; no log interpolation from zero",
                    "code_range": extent,
                }
            )
            continue
        if reason:
            bad_edges.append(
                {
                    "reason": reason,
                    "code_range": extent,
                    "signal_bracket": (left["signal_bounds"][0], right["signal_bounds"][1]),
                }
            )
            excluded.append(extent)
            continue
        interval = tuple(sorted((left["code"], right["code"])))
        valid.append(interval)
        good_edges.append(
            {
                "code_range": interval,
                "left": left,
                "right": right,
                "signal_bracket": (left["signal_bounds"][0], right["signal_bounds"][1]),
            }
        )
    excluded = _merge_ranges(excluded)
    has_support = False
    for low, high in valid:
        pieces = [(low, high)]
        for a, b in excluded:
            pieces = [
                (left_edge, right_edge)
                for lo, hi in pieces
                for left_edge, right_edge in ((lo, min(hi, a)), (max(lo, b), hi))
                if right_edge > left_edge
            ]
        has_support |= any(hi > lo for lo, hi in pieces)
    if not has_support:
        raise UnidentifiableTransfer(
            "no separated positive-reference support remains",
            {"groups": groups, "edges": bad_edges},
        )
    span = max(hi for _, hi in valid) - min(lo for lo, _ in valid)
    nodes = {}
    for edge in good_edges:
        a, b = edge["left"], edge["right"]
        count = max(2, int(np.ceil(lut_points * abs(b["code"] - a["code"]) / span)))
        grid = np.linspace(a["code"], b["code"], count)
        fraction = (grid - a["code"]) / (b["code"] - a["code"])
        values = np.exp(np.log(a["signal"]) + fraction * np.log(b["signal"] / a["signal"]))
        nodes.update(zip(grid.tolist(), (1 + values).tolist(), strict=True))
    ordered = sorted(nodes)
    return MonotonicTransferFit(
        tuple(ordered),
        tuple(nodes[c] for c in ordered),
        _merge_ranges(valid),
        excluded,
        tuple(groups),
        tuple(bad_edges),
        tuple(good_edges),
        {
            "kind": "relative_transmittance",
            "dark_signal": 1.0,
            "white_signal": 2.0,
            "per_column_correction": False,
            "meaning": "dimensionless signal=1+T; not physical dark current",
        },
        "piecewise linear code -> log(T) between separated positive references",
        "declared reference-statistic bounds and monotone signal brackets; "
        "not propagated pixel confidence or a per-sample noise model",
    )
