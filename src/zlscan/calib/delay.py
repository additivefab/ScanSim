"""Estimate transport delay separately from a first-order observable step response."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
from scipy.optimize import least_squares

from zlscan.contracts.events import Derivation, Event
from zlscan.contracts.verdict import Verdict


@dataclass(frozen=True)
class StepResponse:
    delay_ticks: int | None
    registration_ticks: int | None
    response_tau_ticks: float
    normalized_residual_rms: float


def fit_step_response(
    ticks: np.ndarray,
    values: np.ndarray,
    reference_tick: int,
    *,
    reference: Literal["synchronous_injection", "nominal_registration"],
) -> StepResponse:
    """Fit both step polarities; only independent timing identifies physical delay."""
    ticks = np.asarray(ticks)
    values = np.asarray(values, dtype=float)
    if (
        ticks.ndim != 1
        or values.shape != ticks.shape
        or ticks.size < 12
        or not np.issubdtype(ticks.dtype, np.signedinteger)
        or not np.all(np.diff(ticks) > 0)
        or not np.all(np.isfinite(values))
        or not ticks[0] < reference_tick < ticks[-1]
    ):
        raise ValueError(
            "step response requires ordered integer ticks and a reference in the window"
        )
    if reference not in {"synchronous_injection", "nominal_registration"}:
        raise ValueError("unknown timing reference")
    dt = float(np.median(np.diff(ticks)))
    times = (ticks - reference_tick) / dt
    baseline, final = (float(np.median(window)) for window in (values[:4], values[-4:]))
    amplitude = final - baseline
    noise = float(max(np.ptp(values[:4]), np.ptp(values[-4:])))
    if abs(amplitude) <= max(3 * noise, np.finfo(float).eps * max(1, abs(baseline))):
        raise ValueError("step contrast is insufficient")
    normalized = (values - baseline) / amplitude
    crossings = np.flatnonzero(normalized >= 0.5)
    if crossings.size == 0 or not 4 <= crossings[0] < len(ticks) - 4:
        raise ValueError("step response lacks a resolved crossing and plateaus")
    crossing = times[crossings[0]]

    def residual(parameters: np.ndarray) -> np.ndarray:
        delay, tau, low, gain = parameters
        predicted = low - gain * np.expm1(-np.maximum(times - delay, 0) / tau)
        return predicted - normalized

    result = least_squares(
        residual,
        [crossing - 1, 2, 0, 1],
        bounds=([times[2], 0.05, -0.5, 0.25], [times[-4], times[-1] - times[0], 0.5, 2]),
    )
    if not result.success or np.linalg.matrix_rank(result.jac) < 4:
        raise ValueError("fixed delay and response time are not independently identifiable")
    offset = round(result.x[0] * dt)
    return StepResponse(
        delay_ticks=offset if reference == "synchronous_injection" else None,
        registration_ticks=offset if reference == "nominal_registration" else None,
        response_tau_ticks=float(result.x[1] * dt),
        normalized_residual_rms=float(np.sqrt(np.mean(result.fun**2))),
    )


def fit_synchronous_response(
    ticks: np.ndarray,
    values: np.ndarray,
    monitor: Event,
    *,
    session_id: int,
    epoch: int,
) -> StepResponse:
    """Use a measured injection monitor edge as the independent timing reference."""
    if (
        monitor.kind != "injection_monitor"
        or monitor.derivation is not Derivation.SENSOR
        or monitor.recon_verdict() is not Verdict.PASS
        or (monitor.session_id, monitor.epoch) != (session_id, epoch)
    ):
        raise ValueError("physical delay requires an observable injection monitor in this capture")
    return fit_step_response(ticks, values, monitor.tick, reference="synchronous_injection")
