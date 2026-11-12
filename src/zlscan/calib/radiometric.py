"""Logarithmic transfer calibration from dark, white and certified wedge observations."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import least_squares


@dataclass(frozen=True)
class LogTransferFit:
    slope_codes_per_decade: float
    intercept_code: float
    dark_ratio: float
    slope_uncertainty_codes_per_decade: float
    intercept_uncertainty_code: float
    code_knots: tuple[float, ...]
    intensity_knots: tuple[float, ...]
    white_outlier_columns: tuple[int, ...]
    residual_rms_code: float
    reference_transmittance: tuple[float, ...]
    reference_codes: tuple[float, ...]
    fitted_reference_codes: tuple[float, ...]
    reference_residual_codes: tuple[float, ...]


def fit_log_transfer(
    dark: np.ndarray, white: np.ndarray, wedge: np.ndarray, density: np.ndarray
) -> LogTransferFit:
    """Fit code = intercept + slope * log10(T + dark_ratio), with unit white signal."""
    density = np.asarray(density, dtype=float)
    if (
        density.ndim != 1
        or len(density) < 3
        or not np.all(np.isfinite(density))
        or np.any(density < 0)
        or np.any(np.diff(density) <= 0)
    ):
        raise ValueError("wedge requires at least three ordered, distinct finite densities")
    captures = tuple(np.asarray(capture, dtype=float) for capture in (dark, white, wedge))
    if any(
        capture.ndim != 2 or capture.shape[0] < 2 or not np.all(np.isfinite(capture))
        for capture in captures
    ):
        raise ValueError("reference captures require two or more complete finite rows")
    dark, white, wedge = captures
    width = wedge.shape[1]
    if dark.shape[1] != width or white.shape[1] != width or width < 4 * len(density):
        raise ValueError(
            "reference captures require equal width and at least four columns per patch"
        )

    # Patch centres exclude edges, optical blur and small acquisition offsets.
    fraction = ((np.arange(width) + 0.5) / width) * len(density)
    observations = [
        float(np.median(wedge[:, (fraction >= i + 0.25) & (fraction < i + 0.75)]))
        for i in range(len(density))
    ]
    dark_code = float(np.median(dark))
    white_code = float(np.median(white))
    contrast = white_code - dark_code
    if abs(contrast) < 2 or (observations[0] - observations[-1]) * contrast <= 0:
        raise ValueError("reference contrast is insufficient to identify a logarithmic transfer")
    polarity = float(np.sign(contrast))
    t = np.r_[0.0, 1.0, 10**-density]
    codes = np.r_[dark_code, white_code, observations]

    def residual(parameters: np.ndarray) -> np.ndarray:
        offset, log_slope, log_dark = parameters
        slope = polarity * np.exp(log_slope)
        return offset + slope * np.log10(t + 10**log_dark) - codes

    initial_slope = max(1.0, abs(observations[-1] - observations[0]) / np.ptp(density))
    initial_dark = float(np.clip(-abs(contrast) / initial_slope, -11, 0))
    result = least_squares(
        residual,
        [white_code, np.log(initial_slope), initial_dark],
        bounds=([-np.inf, -10, -12], [np.inf, 20, 3]),
        x_scale="jac",
    )
    if not result.success or np.linalg.matrix_rank(result.jac) < 3:
        raise ValueError("logarithmic transfer is unidentifiable")
    offset, log_slope, log_dark = result.x
    slope = polarity * np.exp(log_slope)
    rms = float(np.sqrt(np.mean(result.fun**2)))
    # One-code uncertainty floor includes quantization and finite reference resolution.
    covariance = np.linalg.pinv(result.jac.T @ result.jac) * max(1.0, rms) ** 2
    slope_uncertainty = abs(slope) * np.sqrt(covariance[1, 1])
    offset_uncertainty = np.sqrt(covariance[0, 0])

    white_by_column = np.median(white, axis=0)
    departure = np.abs(white_by_column - white_code)
    threshold = max(3.0, 6 * 1.4826 * float(np.median(departure)))
    flagged = np.flatnonzero(departure > threshold)
    # The applied references are column medians, so include their observed range.
    supported = np.r_[codes, np.median(dark, axis=0), white_by_column[departure <= threshold]]
    knots = np.linspace(float(np.min(supported)), float(np.max(supported)), 257)
    intensities = 10 ** ((knots - offset) / slope)
    return LogTransferFit(
        float(slope),
        float(offset),
        float(10**log_dark),
        float(slope_uncertainty),
        float(offset_uncertainty),
        tuple(knots.tolist()),
        tuple(intensities.tolist()),
        tuple(flagged.tolist()),
        rms,
        tuple(t.tolist()),
        tuple(codes.tolist()),
        tuple((codes + result.fun).tolist()),
        tuple(result.fun.tolist()),
    )


def assess_reference_fit(fit: LogTransferFit, *, tolerance_code: float | None) -> dict:
    """Check this fitted solution, using only an explicitly declared code-domain tolerance."""
    residual = np.asarray(fit.reference_residual_codes, float)
    if residual.ndim != 1 or not len(residual) or not np.all(np.isfinite(residual)):
        raise ValueError("fit reference residuals must be finite and nonempty")
    maximum = float(np.max(np.abs(residual)))
    result = {
        "status": "not_assessed",
        "absolute_tolerance_code": tolerance_code,
        "max_absolute_residual_code": maximum,
        "reference_count": len(residual),
        "violating_reference_indices": None,
        "scope": "fitted reference reproduction; not held-out validation or parameter confidence",
    }
    if tolerance_code is not None:
        if (
            isinstance(tolerance_code, bool)
            or not np.isfinite(tolerance_code)
            or tolerance_code < 0
        ):
            raise ValueError("reference tolerance must be finite and nonnegative")
        indices = np.flatnonzero(np.abs(residual) > tolerance_code).tolist()
        result.update(
            status="mismatch" if indices else "compatible", violating_reference_indices=indices
        )
    return result
