"""Observable-only density reconstruction with local bilinear support in the linear domain."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np

from zlscan.contracts.calibration import Binding, CalibrationHeader, require_applicable
from zlscan.contracts.flags import InvalidFlag


@dataclass(frozen=True)
class Grid:
    width: int
    height: int
    pitch_x_m: float
    pitch_y_m: float
    origin_x_m: float = 0
    origin_y_m: float = 0

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("grid dimensions must be positive")
        geometry = (self.pitch_x_m, self.pitch_y_m, self.origin_x_m, self.origin_y_m)
        if not np.all(np.isfinite(geometry)) or min(self.pitch_x_m, self.pitch_y_m) <= 0:
            raise ValueError("grid pitch must be finite and positive and origin finite")


@dataclass
class DensityImage:
    density: np.ndarray
    transmittance: np.ndarray
    flags: np.ndarray
    support_weight: np.ndarray
    grid: Grid
    calibration_id: str

    @property
    def valid_fraction(self) -> float:
        return float(np.mean(self.flags == 0))


@dataclass(frozen=True)
class _Transfer:
    code_axis: np.ndarray
    intensity_axis: np.ndarray
    dark: np.ndarray
    white_minus_dark: np.ndarray
    valid_white: np.ndarray


def _prepare_transfer(calibration: CalibrationHeader, channel: str, width: int) -> _Transfer:
    try:
        transfer = calibration.items["transfer_lut"].value["channels"][channel]
        dark_codes = calibration.items["dark_raw_code"].value[channel]["median"]
        white_codes = calibration.items["white_raw_code"].value[channel]["median"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"channel {channel} lacks radiometric calibration") from exc
    code_axis = np.asarray(transfer["code_knots"], dtype=float)
    intensity_axis = np.asarray(transfer["intensity_knots"], dtype=float)
    if (
        code_axis.ndim != 1
        or len(code_axis) < 2
        or code_axis.shape != intensity_axis.shape
        or not np.all(np.isfinite(code_axis))
        or not np.all(np.isfinite(intensity_axis))
        or not np.all(np.diff(code_axis) > 0)
        or np.any(intensity_axis <= 0)
    ):
        raise ValueError("invalid code-to-intensity lookup table")
    if np.shape(dark_codes) != (width,) or np.shape(white_codes) != (width,):
        raise ValueError("capture window does not match the reference column count")

    dark = np.interp(dark_codes, code_axis, intensity_axis, left=np.nan, right=np.nan)
    white = np.interp(white_codes, code_axis, intensity_axis, left=np.nan, right=np.nan)
    denominator = white - dark
    valid_white = np.isfinite(denominator) & (denominator > 0)
    outliers = np.asarray(transfer.get("white_outlier_columns", []), dtype=int)
    if np.any((outliers < 0) | (outliers >= width)):
        raise ValueError("white reference flags lie outside the calibrated window")
    valid_white[outliers] = False
    return _Transfer(code_axis, intensity_axis, dark, denominator, valid_white)


def _sample_transmittance(codes: np.ndarray, flags: np.ndarray, transfer: _Transfer) -> np.ndarray:
    signal = np.interp(
        codes, transfer.code_axis, transfer.intensity_axis, left=np.nan, right=np.nan
    )
    flags[~transfer.valid_white] |= int(InvalidFlag.WHITE_REFERENCE_INVALID)
    flags[~np.isfinite(signal)] |= int(InvalidFlag.OUTSIDE_CALIBRATION)
    transmittance = np.full(codes.shape, np.nan)
    np.divide(
        signal - transfer.dark,
        transfer.white_minus_dark,
        out=transmittance,
        where=transfer.valid_white,
    )
    flags[transmittance <= 0] |= int(InvalidFlag.NONPOSITIVE_TRANSMITTANCE)
    return transmittance


def _contributions(
    x: np.ndarray,
    y: np.ndarray | float,
    grid: Grid,
    transmittance: np.ndarray,
    row_flags: np.ndarray,
) -> Iterator[tuple[np.ndarray, ...]]:
    """Yield the nonzero bilinear neighbors in deterministic accumulation order."""
    u = (x - grid.origin_x_m) / grid.pitch_x_m - 0.5
    v = (np.broadcast_to(y, x.shape) - grid.origin_y_m) / grid.pitch_y_m - 0.5
    inside = (u > -1) & (u < grid.width) & (v > -1) & (v < grid.height)
    u, v = u[inside], v[inside]
    sample_t = transmittance[inside]
    reasons = row_flags[inside]
    ix, iy = np.floor(u).astype(int), np.floor(v).astype(int)
    for dy in (0, 1):
        for dx in (0, 1):
            px, py = ix + dx, iy + dy
            weight = (1 - np.abs(px - u)) * (1 - np.abs(py - v))
            used = (px >= 0) & (px < grid.width) & (py >= 0) & (py < grid.height) & (weight > 0)
            if np.any(used):
                yield px[used], py[used], weight[used], sample_t[used], reasons[used]


def reconstruct_density(
    codes: np.ndarray,
    x_m: np.ndarray,
    y_m: np.ndarray,
    grid: Grid,
    calibration: CalibrationHeader,
    capture: Binding,
    *,
    channel: int,
    sample_flags: np.ndarray | None = None,
    min_support: float = 0.5,
    row_gain: np.ndarray | None = None,
    allow_model_calibration: bool = False,
) -> DensityImage:
    """Splat calibrated samples onto pixels; propagate all supported invalid reasons."""
    require_applicable(calibration, capture, allow_model_calibration=allow_model_calibration)
    codes = np.asarray(codes)
    x = np.asarray(x_m, dtype=float)
    y = np.asarray(y_m, dtype=float)
    if codes.ndim != 2 or codes.shape != x.shape or codes.shape != y.shape:
        raise ValueError("ADC codes and sample coordinates must have matching 2-D shapes")
    if not np.isfinite(min_support) or min_support <= 0:
        raise ValueError("minimum support must be finite and positive")
    flags = np.zeros(codes.shape, dtype=np.uint16)
    if sample_flags is not None:
        if sample_flags.shape != codes.shape:
            raise ValueError("sample quality flags must match the ADC code shape")
        flags[...] = sample_flags

    if row_gain is not None:
        row_gain = np.asarray(row_gain, dtype=float)
        if row_gain.shape != (len(codes),) or not np.all(np.isfinite(row_gain) & (row_gain > 0)):
            raise ValueError("facet gain must be positive and defined for every row")

    transfer = _prepare_transfer(calibration, str(channel), codes.shape[1])
    output_shape = (grid.height, grid.width)
    total = np.zeros(output_shape)
    support = np.zeros(output_shape)
    pixel_flags = np.zeros(output_shape, dtype=np.uint16)
    for row in range(len(codes)):
        row_flags = flags[row]
        transmittance = _sample_transmittance(codes[row], row_flags, transfer)
        if row_gain is not None:
            transmittance /= row_gain[row]
        for px, py, weight, sample_t, reasons in _contributions(
            x[row], y[row], grid, transmittance, row_flags
        ):
            np.bitwise_or.at(pixel_flags, (py, px), reasons)
            valid = (reasons == 0) & np.isfinite(sample_t)
            np.add.at(support, (py[valid], px[valid]), weight[valid])
            np.add.at(total, (py[valid], px[valid]), weight[valid] * sample_t[valid])

    pixel_flags[support < min_support] |= int(InvalidFlag.RESAMPLING_SUPPORT)
    valid = pixel_flags == 0
    transmittance = np.full(output_shape, np.nan)
    np.divide(total, support, out=transmittance, where=valid)
    density = -np.log10(transmittance)
    return DensityImage(
        density, transmittance, pixel_flags, support, grid, calibration.calibration_id
    )
