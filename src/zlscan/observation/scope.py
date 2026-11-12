"""Explicit candidate probe/scope response sampled on an external time coordinate.

First-order shapes are assumptions, not inferred from a bandwidth label. Continuous
states compose all stages before exact piecewise-linear propagation. No truth access.
"""

from __future__ import annotations

import math
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field
from scipy.linalg import expm


class ScopeObserverConfig(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, allow_inf_nan=False, str_strip_whitespace=True
    )
    version: Literal[1] = 1
    parameter_source: str | None = Field(default=None, min_length=1)
    probe_ratio: float | None = Field(default=None, gt=0)
    probe_compensated: bool | None = None
    probe_bandwidth_hz: float | None = Field(default=None, ge=0)
    scope_bandwidth_hz: float | None = Field(default=None, ge=0)
    bandwidth_limit_hz: float | None = Field(default=None, ge=0)
    coupling: Literal["DC", "AC"] | None = None
    ac_corner_hz: float | None = Field(default=None, gt=0)
    aperture_s: float | None = Field(default=None, ge=0)
    initial_state: Literal["zero", "steady"] | None = None
    time_offset_s: float | None = None
    alignment_uncertainty_s: float | None = Field(default=None, ge=0)

    def require_known(self) -> None:
        required = [
            "parameter_source",
            "probe_ratio",
            "probe_compensated",
            "probe_bandwidth_hz",
            "scope_bandwidth_hz",
            "bandwidth_limit_hz",
            "coupling",
            "aperture_s",
            "initial_state",
            "time_offset_s",
        ]
        if self.coupling == "AC":
            required.append("ac_corner_hz")
        missing = [k for k in required if getattr(self, k) is None]
        if missing:
            raise ValueError(f"unknown observer parameters: {missing}")
        if self.coupling == "DC" and self.ac_corner_hz is not None:
            raise ValueError("AC corner is not applicable to DC coupling")


def _system(config: ScopeObserverConfig) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    a, b, c, d = np.zeros((0, 0)), np.zeros(0), np.zeros(0), 1.0 / config.probe_ratio

    def pole(fc_hz: float, highpass: bool = False) -> None:
        nonlocal a, b, c, d
        if fc_hz == 0:
            return
        k = 2 * math.pi * fc_hz
        if not math.isfinite(k):
            raise ValueError("observer rate is nonfinite")
        n = len(b)
        expanded = np.zeros((n + 1, n + 1))
        expanded[:n, :n] = a
        expanded[n, :n], expanded[n, n] = k * c, -k
        a, b = expanded, np.r_[b, k * d]
        if highpass:
            c = np.r_[c, -1.0]
        else:
            c, d = np.r_[np.zeros(n), 1.0], 0.0

    pole(config.probe_bandwidth_hz)
    if config.coupling == "AC":
        pole(config.ac_corner_hz, highpass=True)
    pole(config.scope_bandwidth_hz)
    pole(config.bandwidth_limit_hz)
    if config.probe_compensated:
        c, d = c * config.probe_ratio, d * config.probe_ratio
    if not all(np.isfinite(v).all() for v in [a, b, c, np.asarray(d)]):
        raise ValueError("observer system is nonfinite")
    return a, b, c, d


def observe_voltage(
    time_s: np.ndarray,
    voltage_v: np.ndarray,
    sample_time_s: np.ndarray,
    config: ScopeObserverConfig,
) -> dict:
    """Sample one explicit candidate response, refusing extrapolation and unknown hardware.

    Source interpolation is linear. Apertures are centred rectangular averages.
    Initial state and scope-to-model offset are declarations, never fitted here.
    Limits bound this batch comparison; no claim about long-running capture speed.
    """
    config.require_known()
    t, u, q = (np.asarray(v, dtype=np.float64) for v in [time_s, voltage_v, sample_time_s])
    if (
        any(v.ndim != 1 for v in [t, u, q])
        or len(t) != len(u)
        or len(t) < 2
        or not len(q)
        or len(t) > 20_000
        or len(q) > 20_000
        or any(not np.isfinite(v).all() for v in [t, u, q])
        or not (np.diff(t) > 0).all()
    ):
        raise ValueError(
            "finite 1D source (2..20000 knots), samples (1..20000), increasing time required"
        )
    with np.errstate(over="ignore", invalid="ignore"):
        mapped = q + config.time_offset_s
        left, right = mapped - config.aperture_s / 2, mapped + config.aperture_s / 2
    if not np.isfinite(left).all() or not np.isfinite(right).all():
        raise ValueError("mapped sample times are nonfinite")
    invalid = [None] * len(q)
    for i, (lo, hi) in enumerate(zip(left, right, strict=True)):
        if lo < t[0] or hi > t[-1]:
            invalid[i] = "outside_model_support"
        elif config.aperture_s > 0 and hi <= lo:
            invalid[i] = "aperture_below_time_resolution"
    valid = [i for i, reason in enumerate(invalid) if reason is None]
    a, b, c, d = _system(config)
    n = len(b)
    generator = np.zeros((n + 3, n + 3))
    generator[:n, :n], generator[:n, n] = a, b
    generator[n, n + 1] = 1.0
    generator[n + 2, :n], generator[n + 2, n] = c, d
    cache = {}

    def advance(state: np.ndarray, lo: float, hi: float) -> tuple[np.ndarray, float]:
        dt = hi - lo
        first, last = np.interp([lo, hi], t, u)
        with np.errstate(over="ignore", invalid="ignore"):
            slope = (last - first) / dt
        if not math.isfinite(slope):
            raise ValueError("source slope is nonfinite at observation resolution")
        matrix = cache.get(dt)
        if matrix is None:
            matrix = expm(generator * dt)
            if len(cache) < 512:
                cache[dt] = matrix
        result = matrix @ np.r_[state, first, slope, 0.0]
        if not np.isfinite(result).all():
            raise ValueError("observer propagation is nonfinite")
        return result[:n], float(result[n + 2])

    states = {}
    if valid:
        events = np.unique(np.r_[t, left[valid]])
        state = np.zeros(n)
        if config.initial_state == "steady" and n:
            state = np.linalg.solve(a, -b * u[0])
        states[float(events[0])] = state.copy()
        for lo, hi in zip(events[:-1], events[1:], strict=True):
            state, _ = advance(state, float(lo), float(hi))
            states[float(hi)] = state.copy()
    prediction: list[float | None] = [None] * len(q)
    work = 0
    for i in valid:
        lo, hi = float(left[i]), float(right[i])
        state = states[lo].copy()
        if config.aperture_s == 0:
            value = float(c @ state + d * np.interp(lo, t, u))
        else:
            grid = np.r_[lo, t[(t > lo) & (t < hi)], hi]
            work += len(grid) - 1
            if work > 2_000_000:
                raise ValueError("aperture integration exceeds batch work limit")
            pieces = []
            for start, end in zip(grid[:-1], grid[1:], strict=True):
                state, area = advance(state, float(start), float(end))
                pieces.append(area)
            # Normalize by represented width to retain DC even at large time origins.
            value = math.fsum(pieces) / (hi - lo)
        if not math.isfinite(value):
            raise ValueError("observer output is nonfinite")
        prediction[i] = value
    return {
        "format": "zlscan.scope-observation/1",
        "origin": "MODEL_ESTIMATE",
        "response": "assumed_first_order_cascade",
        "config": config.model_dump(mode="json"),
        "sample_time_s": q.tolist(),
        "model_time_s": mapped.tolist(),
        "voltage_v": prediction,
        "invalid_reason": invalid,
        "source_interpolation": "linear",
        "sample_aperture": "point" if config.aperture_s == 0 else "centred_rectangular",
        "alignment_uncertainty_s": config.alignment_uncertainty_s,
        "hard_acceptance_eligible": False,
        "not_modeled": ["probe_loading", "instrument_noise", "quantization", "clock_jitter"],
    }
