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
    grid: list[Landmark] = []
    slanted: list[Landmark] = []
    slanted_dx_dy: FiniteFloat | None = None


def fit_observed_targets(
    path: Path,
    raw: ReadResult,
    digest: str,
    flags_by_channel: dict[int, np.ndarray],
    grid_lines: range,
    slanted_lines: range,
    codes_by_channel: dict[int, np.ndarray],
    grid_fractions: list[float],
    width_m: float,
    dark_lines: range,
    white_lines: range,
) -> tuple[dict, np.ndarray]:
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

    grid = targets.grid
    if not grid:
        codes = codes_by_channel[targets.channel]
        dark, white = (
            float(np.median(codes[region.start : region.stop]))
            for region in (dark_lines, white_lines)
        )
        grid = _detect_grid(
            codes, flags, grid_lines, grid_fractions, width_m, np.sign(dark - white)
        )
    phase, u, x = points(grid, grid_lines)
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
    model = GeometryModel(policy=targets.timing.policy, window=raw.session.windows, facets=result)
    return model.model_dump(mode="json"), phases


def fit_facet_gain(
    white_codes: np.ndarray,
    phases: np.ndarray,
    dark_codes: np.ndarray,
    transfer: dict,
) -> dict[str, float]:
    """Estimate each locked phase's white amplitude relative to the global white reference."""
    slope = transfer["slope_codes_per_decade"]
    intercept = transfer["intercept_code"]
    base = 10 ** ((np.median(white_codes, axis=0) - intercept) / slope)
    dark = 10 ** ((dark_codes - intercept) / slope)
    span = base - dark
    valid = np.isfinite(span) & (span > 0)
    valid[list(transfer["white_outlier_columns"])] = False
    gains = {}
    for phase in np.unique(phases[phases >= 0]):
        if not np.any(valid):
            break
        group_white = np.median(white_codes[phases == phase], axis=0)
        intensity = 10 ** ((group_white - intercept) / slope)
        ratios = (intensity[valid] - dark[valid]) / span[valid]
        gain = float(np.median(ratios))
        if np.isfinite(gain) and gain > 0:
            gains[str(phase)] = gain
    return gains


def _detect_grid(
    codes: np.ndarray,
    flags: np.ndarray,
    lines: range,
    fractions: list[float],
    width_m: float,
    dark_polarity: float,
) -> list[Landmark]:
    """Locate certified dark bars at the midpoint of their two half-contrast crossings."""
    centers = np.asarray(fractions, dtype=float) * codes.shape[1] - 0.5
    if (
        len(centers) < 3
        or not np.all(np.isfinite(centers))
        or not 0 < centers[0] < centers[-1] < codes.shape[1] - 1
        or np.any(np.diff(centers) < 6)
        or dark_polarity == 0
    ):
        raise ValueError(
            "automatic grid detection requires separated certified bars and dark contrast"
        )
    radius = 0.4 * float(np.min(np.diff(centers)))
    result = []
    for line in lines:
        oriented = dark_polarity * codes[line].astype(float)
        for fraction, center in zip(fractions, centers, strict=True):
            start = max(0, int(np.ceil(center - radius)))
            stop = min(len(oriented), int(np.floor(center + radius)) + 1)
            values = oriented[start:stop]
            if len(values) < 5 or flags[line, start:stop].any():
                continue
            peak = int(np.argmax(values))
            baseline = float(np.median(np.r_[values[:2], values[-2:]]))
            height = values[peak] - baseline
            if height <= 3:
                continue
            level = baseline + height / 2
            below_left = np.flatnonzero(values[:peak] < level)
            below_right = np.flatnonzero(values[peak + 1 :] < level)
            if not len(below_left) or not len(below_right):
                continue
            left = int(below_left[-1])
            right = peak + 1 + int(below_right[0])
            x_left = left + (level - values[left]) / (values[left + 1] - values[left])
            x_right = right - (level - values[right]) / (values[right - 1] - values[right])
            result.append(
                Landmark(
                    line=line,
                    sample_column=start + (x_left + x_right) / 2,
                    target_x_m=fraction * width_m,
                )
            )
    return result
