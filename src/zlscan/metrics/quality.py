"""Image metrics with explicit, shared regions of evaluation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np

from zlscan.contracts.flags import InvalidFlag


@dataclass(frozen=True)
class DensityComparison:
    metrics: dict
    evaluation_mask: np.ndarray


def compare_density(
    density: np.ndarray,
    flags: np.ndarray,
    references: Mapping[str, np.ndarray],
    region: np.ndarray,
) -> DensityComparison:
    """Measure both references on the same finite, valid pixels."""
    density = np.asarray(density, dtype=float)
    flags = np.asarray(flags)
    roi = np.asarray(region, dtype=bool)
    reference_arrays = {name: np.asarray(ref, dtype=float) for name, ref in references.items()}
    if (
        density.ndim != 2
        or not reference_arrays
        or any(a.shape != density.shape for a in (flags, roi, *reference_arrays.values()))
    ):
        raise ValueError("image, flags, references and region must have identical 2-D shapes")
    total = int(np.count_nonzero(roi))
    if not total:
        raise ValueError("evaluation region is empty")
    common = roi & (flags == 0) & np.isfinite(density)
    for ref in reference_arrays.values():
        common &= np.isfinite(ref)
    valid = int(np.count_nonzero(common))
    per_reference = {}
    for name, ref in reference_arrays.items():
        residual = density[common] - ref[common]
        if not valid:
            per_reference[name] = {"mse": None, "rmse": None, "bias": None, "variance": None}
            continue
        mse = float(np.mean(residual**2))
        per_reference[name] = {
            "mse": mse,
            "rmse": float(np.sqrt(mse)),
            "bias": float(np.mean(residual)),
            "variance": float(np.var(residual)),
        }
    counts = {
        flag.name: int(np.count_nonzero(roi & ((flags & int(flag)) != 0))) for flag in InvalidFlag
    }
    return DensityComparison(
        {
            "references": per_reference,
            "region_pixels": total,
            "valid_pixels": valid,
            "valid_fraction": valid / total,
            "invalid_reason_counts": counts,
            "missing_rows": int(
                np.count_nonzero(
                    np.any(roi, axis=1)
                    & np.all(~roi | ((flags & int(InvalidFlag.MISSING_SAMPLE)) != 0), axis=1)
                )
            ),
        },
        common,
    )


def wedge_statistics(
    density: np.ndarray,
    labels: np.ndarray,
    levels: np.ndarray,
    mask: np.ndarray,
) -> dict:
    """Measure per-step bias, noise, usable range and density linearity."""
    image, labels, levels = np.asarray(density), np.asarray(labels), np.asarray(levels)
    if image.shape != labels.shape or mask.shape != image.shape or levels.ndim != 1:
        raise ValueError("wedge labels and mask must match the image, with one vector of levels")
    rows = []
    for index, level in enumerate(levels):
        values = image[mask & (labels == index) & np.isfinite(image)]
        rows.append(
            {
                "target_od": float(level),
                "pixels": len(values),
                "mean_od": float(np.mean(values)) if len(values) else None,
                "bias_od": float(np.mean(values) - level) if len(values) else None,
                "std_od": float(np.std(values, ddof=1)) if len(values) > 1 else None,
            }
        )
    usable = [row for row in rows if row["mean_od"] is not None]
    linearity = None
    if len(usable) >= 2:
        x = np.array([row["target_od"] for row in usable])
        y = np.array([row["mean_od"] for row in usable])
        slope, offset = np.polyfit(x, y, 1)
        linearity = {
            "slope": float(slope),
            "offset_od": float(offset),
            "max_residual_od": float(np.max(np.abs(y - (slope * x + offset)))),
        }
    return {
        "steps": rows,
        "linearity": linearity,
        "covered_target_range_od": [usable[0]["target_od"], usable[-1]["target_od"]]
        if usable
        else None,
        "highest_step_mean_od": rows[-1]["mean_od"] if rows else None,
    }


def uniform_noise_psd(patches: np.ndarray, pitch_x_m: float, pitch_y_m: float) -> np.ndarray:
    """Two-sided spatial PSD averaged over complete uniform patches."""
    patches = np.asarray(patches, dtype=float)
    if (
        patches.ndim != 3
        or min(patches.shape) < 1
        or not np.all(np.isfinite(patches))
        or not np.all(np.isfinite([pitch_x_m, pitch_y_m]))
        or min(pitch_x_m, pitch_y_m) <= 0
    ):
        raise ValueError("PSD requires finite patches and positive finite pixel pitches")
    centred = patches - np.mean(patches, axis=(-2, -1), keepdims=True)
    spectrum = np.fft.fft2(centred, axes=(-2, -1))
    area = patches.shape[-1] * patches.shape[-2]
    return np.mean(np.abs(spectrum) ** 2, axis=0) * (pitch_x_m * pitch_y_m / area)


def slanted_edge_mtf(
    image: np.ndarray,
    distance_to_edge_m: np.ndarray,
    mask: np.ndarray,
    bin_width_m: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Bin edge-normal distances, differentiate the ESF and transform the line spread."""
    image, distance = np.asarray(image, dtype=float), np.asarray(distance_to_edge_m, dtype=float)
    if (
        image.ndim != 2
        or distance.shape != image.shape
        or mask.shape != image.shape
        or not np.isfinite(bin_width_m)
        or bin_width_m <= 0
    ):
        raise ValueError("MTF requires matching image, distance and mask plus positive bin width")
    used = mask & np.isfinite(image) & np.isfinite(distance)
    if np.count_nonzero(used) < 16:
        raise ValueError("MTF has insufficient edge samples")
    coordinate = distance[used]
    index = np.rint((coordinate - np.min(coordinate)) / bin_width_m).astype(int)
    if np.max(index) > 4 * len(index):
        raise ValueError("edge bins are too sparse")
    counts = np.bincount(index)
    if len(counts) < 8 or np.any(counts == 0):
        raise ValueError("edge bins lack contiguous sample support")
    esf = np.bincount(index, weights=image[used]) / counts
    lsf = np.diff(esf)
    transfer = np.abs(np.fft.rfft(lsf))
    if transfer[0] <= np.finfo(float).eps * max(1.0, float(np.max(np.abs(esf)))):
        raise ValueError("edge contrast is insufficient")
    frequencies = np.fft.rfftfreq(len(lsf), bin_width_m)
    return frequencies, transfer / transfer[0]


def facet_banding(
    row_levels: np.ndarray, facet_phase: np.ndarray, edge_positions_m: np.ndarray
) -> dict:
    """Compare observed facet groups and separate edge roughness from group offsets."""
    levels, phase, edges = map(np.asarray, (row_levels, facet_phase, edge_positions_m))
    if levels.ndim != 1 or levels.shape != phase.shape or levels.shape != edges.shape:
        raise ValueError("banding requires equally sized one-dimensional row observations")
    valid = (phase >= 0) & np.isfinite(levels) & np.isfinite(edges)
    group_offsets = {}
    residual_edges = []
    baseline = float(np.mean(levels[valid])) if np.any(valid) else None
    for group in np.unique(phase[valid]):
        selected = valid & (phase == group)
        group_offsets[str(int(group))] = float(np.mean(levels[selected]) - baseline)
        residual_edges.extend(edges[selected] - np.mean(edges[selected]))
    return {
        "facet_level_offsets": group_offsets,
        "edge_roughness_rms_m": (
            float(np.sqrt(np.mean(np.square(residual_edges)))) if residual_edges else None
        ),
        "unlocked_rows": int(np.count_nonzero(phase < 0)),
    }


def geometry_error(actual_xy_m: np.ndarray, target_xy_m: np.ndarray, row_y_m: np.ndarray) -> dict:
    """Summarize point errors, inter-row spacing and residual scan skew."""
    actual = np.asarray(actual_xy_m, dtype=float)
    target = np.asarray(target_xy_m, dtype=float)
    row_y = np.asarray(row_y_m, dtype=float)
    if (
        actual.ndim != 2
        or actual.shape[1] != 2
        or actual.shape != target.shape
        or len(actual) < 2
        or row_y.ndim != 1
        or len(row_y) < 2
        or any(not np.all(np.isfinite(a)) for a in (actual, target, row_y))
        or np.any(np.diff(row_y) <= 0)
    ):
        raise ValueError("geometry requires paired finite points and ordered row positions")
    errors = actual - target
    spacing = np.diff(row_y)
    skew = None
    skew_residual = None
    if np.ptp(target[:, 1]) > 0:
        slope, offset = np.polyfit(target[:, 1], errors[:, 0], 1)
        skew = float(slope)
        skew_residual = float(np.sqrt(np.mean((errors[:, 0] - offset - slope * target[:, 1]) ** 2)))
    return {
        "point_rms_m": float(np.sqrt(np.mean(np.sum(errors**2, axis=1)))),
        "row_spacing_mean_m": float(np.mean(spacing)),
        "row_spacing_std_m": float(np.std(spacing)),
        "scan_skew_dx_dy": skew,
        "scan_skew_residual_rms_m": skew_residual,
    }


def step_recovery(values: np.ndarray, edge_sample: int, tolerance_fraction: float = 0.02) -> dict:
    """Measure normalized settling from a declared edge with plateaus at both ends."""
    values = np.asarray(values, dtype=float)
    if (
        values.ndim != 1
        or not 2 <= edge_sample < len(values) - 2
        or not np.all(np.isfinite(values))
        or not 0 < tolerance_fraction < 1
    ):
        raise ValueError("step recovery requires finite observations and a resolved edge")
    initial = float(np.median(values[:edge_sample]))
    final = float(np.median(values[-max(2, (len(values) - edge_sample) // 5) :]))
    amplitude = abs(final - initial)
    if amplitude == 0:
        raise ValueError("step recovery is unidentifiable without contrast")
    residual = np.abs(values[edge_sample:] - final) / amplitude
    unsettled = np.flatnonzero(residual > tolerance_fraction)
    settling = int(unsettled[-1]) + 1 if len(unsettled) else 0
    return {
        "settling_samples": settling if settling < len(residual) else None,
        "normalized_tail_area_sample": float(np.sum(residual)),
        "tolerance_fraction": tolerance_fraction,
    }
