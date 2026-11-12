"""Observable facet phase tracking and per-phase geometric calibration."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from zlscan.io.facet_timing import infer_facet_phase as infer_facet_phase


@dataclass(frozen=True)
class FacetGeometry:
    x_coefficients_m: tuple[float, ...] | None
    x_residual_rms_m: float | None
    y_offset_m: float | None
    y_status: str
    gain: float | None


def fit_facet_geometry(
    phases: np.ndarray,
    normalized_time: np.ndarray,
    grid_x_m: np.ndarray,
    slanted_x_residual_m: np.ndarray | None,
    slanted_dx_dy: float | None,
    white_intensity: np.ndarray | None,
) -> dict[int, FacetGeometry]:
    """Estimate x(u)=a+b*u+c*u³ and infer y only from an independent slanted target."""
    phases = np.asarray(phases)
    t = np.asarray(normalized_time, dtype=float)
    x = np.asarray(grid_x_m, dtype=float)
    slanted = (
        None if slanted_x_residual_m is None else np.asarray(slanted_x_residual_m, dtype=float)
    )
    white = None if white_intensity is None else np.asarray(white_intensity, dtype=float)
    if phases.ndim != 1 or not np.issubdtype(phases.dtype, np.signedinteger):
        raise ValueError("facet phase observations must be a one-dimensional integer array")
    if any(
        array.shape != phases.shape or not np.all(np.isfinite(array))
        for array in (t, x, slanted, white)
        if array is not None
    ):
        raise ValueError("facet observations must have matching dimensions and finite values")
    if (slanted is None) != (slanted_dx_dy is None):
        raise ValueError("slanted residuals require a known target slope")
    if slanted_dx_dy is not None and not (np.isfinite(slanted_dx_dy) and slanted_dx_dy != 0):
        raise ValueError("slanted target slope must be finite and nonzero")
    if not np.any(phases >= 0):
        return {}

    reference_white = None
    if white is not None:
        reference_white = float(np.median(white[phases >= 0]))
        if reference_white <= 0:
            raise ValueError("white intensity must have a positive reference level")

    fits = {}
    for phase in np.unique(phases[phases >= 0]):
        selected = phases == phase
        u = t[selected]
        design = np.column_stack((np.ones_like(u), u, u**3))
        coefficients, _, rank, _ = np.linalg.lstsq(design, x[selected], rcond=None)
        fitted_x = tuple(coefficients.tolist()) if rank == 3 else None
        residual = float(np.sqrt(np.mean((design @ coefficients - x[selected]) ** 2)))
        dy = None
        if slanted is not None and slanted_dx_dy is not None and fitted_x is not None:
            dy = float(np.median(slanted[selected])) / slanted_dx_dy
        gain = None
        if white is not None and reference_white is not None:
            gain = float(np.median(white[selected])) / reference_white
        fits[int(phase)] = FacetGeometry(
            fitted_x,
            residual if fitted_x is not None else None,
            dy,
            "ok" if dy is not None else "unidentifiable",
            gain,
        )
    return fits
