"""Fit geometric calibration from operator-identified sample landmarks and certified targets."""

from pathlib import Path

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, StrictInt

from zlscan.calib.geometry import fit_facet_geometry
from zlscan.io.facet_timing import FacetMap, FacetTiming, GeometryModel, observed_phases
from zlscan.io.rawstream import ReadResult


class Landmark(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line: StrictInt = Field(ge=0)
    sample_column: FiniteFloat = Field(ge=0)
    target_x_m: FiniteFloat


class FacetTargets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timing: FacetTiming
    channel: StrictInt = Field(ge=0)
    grid: list[Landmark]
    slanted: list[Landmark] = []
    slanted_dx_dy: FiniteFloat | None = None


def fit_observed_targets(
    path: Path,
    raw: ReadResult,
    digest: str,
    flags_by_channel: dict[int, np.ndarray],
    grid_lines: range,
    slanted_lines: range,
) -> dict:
    targets = FacetTargets.model_validate_json(path.read_bytes())
    if targets.channel not in flags_by_channel:
        raise ValueError("facet targets reference an absent channel")
    flags = flags_by_channel[targets.channel]
    phases = observed_phases(targets.timing, raw, digest, flags.shape[0])
    columns = flags.shape[1]
    if bool(targets.slanted) != (targets.slanted_dx_dy is not None) or targets.slanted_dx_dy == 0:
        raise ValueError("slanted landmarks require a nonzero certified slope")

    def points(landmarks: list[Landmark], allowed: range) -> tuple[np.ndarray, ...]:
        rows = []
        for point in landmarks:
            col = point.sample_column
            if point.line not in allowed or col > columns - 1:
                raise ValueError("facet landmark lies outside its recorded target")
            if flags[point.line, int(np.floor(col)) : int(np.ceil(col)) + 1].any():
                raise ValueError("quality events invalidate a facet landmark")
            rows.append((phases[point.line], 2 * (col + 0.5) / columns - 1, point.target_x_m))
        phase, u, x = np.asarray(rows, dtype=float).reshape(-1, 3).T
        return phase.astype(int), u, x

    phase, u, x = points(targets.grid, grid_lines)
    sp, su, sx = points(targets.slanted, slanted_lines)
    result = {}
    for facet, fit in fit_facet_geometry(phase, u, x, None, None, None).items():
        coefficients = fit.x_coefficients_m
        if coefficients is None:
            continue
        a, b, c = coefficients
        support = u[phase == facet]
        low, high = float(support.min()), float(support.max())
        chosen = (sp == facet) & (su >= low) & (su <= high)
        dy = None
        if np.any(chosen):
            values = su[chosen]
            dy = (
                float(np.median(a + b * values + c * values**3 - sx[chosen]))
                / targets.slanted_dx_dy
            )
        result[str(facet)] = FacetMap(
            x_coefficients_m=coefficients,
            x_residual_rms_m=fit.x_residual_rms_m,
            support_u=(low, high),
            y_offset_m=dy,
        )
    if not result:
        raise ValueError("facet landmarks did not identify a scan-direction mapping")
    return GeometryModel(
        policy=targets.timing.policy, window=raw.session.windows, facets=result
    ).model_dump(mode="json")
