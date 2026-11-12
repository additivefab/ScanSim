"""Evaluate declared targets on saved image masks; evaluation inputs never enter reconstruction."""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np

from zlscan.io.manifest import sha256_file
from zlscan.metrics.quality import (
    facet_banding,
    geometry_error,
    slanted_edge_mtf,
    step_recovery,
    uniform_noise_psd,
    wedge_statistics,
)


def _number(value, label: str, *, positive: bool = False) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not np.isfinite(value)
        or (positive and value <= 0)
    ):
        raise ValueError(f"{label} must be {'positive ' if positive else ''}finite numeric")
    return float(value)


def _integer(value, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _unavailable(reason: str) -> dict:
    return {"status": "not_computed", "reason": reason}


def evaluate_targets(
    path: Path,
    density: np.ndarray,
    common: np.ndarray,
) -> tuple[dict, dict[str, np.ndarray], dict[str, str]]:
    """Return metric results, exact per-target masks, and hashes of every target input."""
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("format") != "zlscan-quality-targets/1":
        raise ValueError("unsupported quality targets format")
    requests = document.get("evaluations")
    if not isinstance(requests, list) or not requests:
        raise ValueError("evaluations must be a non-empty list")
    names = [r.get("name") if isinstance(r, dict) else None for r in requests]
    if any(not isinstance(n, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", n) for n in names):
        raise ValueError("metric names must be non-empty ASCII identifiers")
    if len(set(names)) != len(names):
        raise ValueError("metric names must be unique")
    density, common = np.asarray(density, dtype=float), np.asarray(common)
    if density.ndim != 2 or common.shape != density.shape or common.dtype != bool:
        raise ValueError("density and boolean common mask must have matching 2-D shapes")
    if np.any(common & ~np.isfinite(density)):
        raise ValueError("common mask includes non-finite density")
    inputs = {str(path.resolve()): sha256_file(path)}

    def array(request: dict, key: str) -> np.ndarray:
        filename = request[key]
        if not isinstance(filename, str):
            raise ValueError(f"{key} must be a .npy array path")
        source = (path.parent / filename).resolve()
        if source.suffix != ".npy":
            raise ValueError(f"{key} must be a .npy array path")
        value = np.load(source, allow_pickle=False)
        if value.dtype.kind not in "biuf":
            raise ValueError(f"{key} must contain real numeric or boolean data")
        inputs[str(source)] = sha256_file(source)
        return value

    results, masks = {}, {}
    for request in requests:
        name, kind = request["name"], request["kind"]
        mask = common.copy()
        if "region" in request:
            region = array(request, "region")
            if region.dtype != bool or region.shape != density.shape:
                raise ValueError("target region must be a boolean array matching the image")
            mask &= region
        if kind == "wedge":
            labels = array(request, "labels")
            levels = np.asarray(request["levels_od"], dtype=float)
            if (
                labels.shape != density.shape
                or not np.issubdtype(labels.dtype, np.integer)
                or levels.ndim != 1
                or len(levels) < 2
                or not np.all(np.isfinite(levels))
                or np.any(np.diff(levels) <= 0)
            ):
                raise ValueError("wedge needs matching integer labels and increasing finite levels")
            mask &= (labels >= 0) & (labels < len(levels))
            result = wedge_statistics(density, labels, levels, mask)
            for index, row in enumerate(result["steps"]):
                values = np.power(10.0, -density[mask & (labels == index)])
                if not np.all(np.isfinite(values)):
                    raise ValueError("transmittance exceeds finite numeric range")
                noise = float(np.std(values, ddof=1)) if len(values) > 1 else None
                signal = float(np.mean(values)) if len(values) else None
                row["transmittance_snr_db"] = (
                    float(20 * (np.log10(signal) - np.log10(noise)))
                    if noise and signal and signal > 0
                    else None
                )
                row["snr_status"] = (
                    "computed"
                    if row["transmittance_snr_db"] is not None
                    else "zero_noise"
                    if noise == 0
                    else "insufficient_support"
                )
            result["usable_target_range_od"] = None
            result["usable_target_levels_od"] = None
            if "max_abs_bias_od" in request or "max_std_od" in request:
                bias = _number(request["max_abs_bias_od"], "max_abs_bias_od", positive=True)
                std = _number(request["max_std_od"], "max_std_od", positive=True)
                usable_indices = [
                    index
                    for index, r in enumerate(result["steps"])
                    if r["std_od"] is not None and abs(r["bias_od"]) <= bias and r["std_od"] <= std
                ]
                usable = [float(levels[index]) for index in usable_indices]
                result["usability_limits"] = {"max_abs_bias_od": bias, "max_std_od": std}
                result["usable_target_levels_od"] = usable
                if usable and np.all(np.diff(usable_indices) == 1):
                    result["usable_target_range_od"] = [min(usable), max(usable)]
        elif kind == "mtf":
            if request["direction"] not in {"scan", "feed"}:
                raise ValueError("MTF direction must be scan or feed")
            working = _number(request["working_density_od"], "working_density_od")
            speed = _number(request["scan_speed_m_s"], "scan_speed_m_s", positive=True)
            width = _number(request["bin_width_m"], "bin_width_m", positive=True)
            distance = array(request, "distance_m")
            if distance.shape != density.shape:
                raise ValueError("edge distance must match the image")
            mask &= np.isfinite(distance)
            transmission = np.full(density.shape, np.nan)
            transmission[mask] = np.power(10.0, -density[mask])
            if np.any(mask & ~np.isfinite(transmission)):
                raise ValueError("transmittance exceeds finite numeric range")
            try:
                frequency, mtf = slanted_edge_mtf(transmission, distance, mask, width)
                result = {
                    "frequency_cycles_m": frequency.tolist(),
                    "mtf": mtf.tolist(),
                    "direction": request["direction"],
                    "domain": "transmittance",
                    "working_density_od": working,
                    "scan_speed_m_s": speed,
                }
            except ValueError as exc:
                result = _unavailable(str(exc))
        elif kind == "noise":
            pitch_x = _number(request["pitch_x_m"], "pitch_x_m", positive=True)
            pitch_y = _number(request["pitch_y_m"], "pitch_y_m", positive=True)
            rectangles = request["patches"]
            if not isinstance(rectangles, list) or not rectangles:
                raise ValueError("noise patches must be a non-empty list of rectangles")
            patches, shape = [], None
            used = np.zeros(density.shape, bool)
            for rectangle in rectangles:
                if not isinstance(rectangle, list) or len(rectangle) != 4:
                    raise ValueError("noise rectangle must be [y0,y1,x0,x1]")
                y0, y1, x0, x1 = [_integer(v, "rectangle bound") for v in rectangle]
                if not (0 <= y0 < y1 <= density.shape[0] and 0 <= x0 < x1 <= density.shape[1]):
                    raise ValueError("noise rectangle is outside the image")
                patch_shape = (y1 - y0, x1 - x0)
                shape = shape or patch_shape
                if shape != patch_shape:
                    raise ValueError("noise rectangles must have identical shapes")
                if np.all(mask[y0:y1, x0:x1]):
                    patches.append(density[y0:y1, x0:x1])
                    used[y0:y1, x0:x1] = True
            mask = used
            result = {
                "accepted_patches": len(patches),
                "rejected_patches": len(rectangles) - len(patches),
                "domain": "density",
            }
            if patches:
                psd = uniform_noise_psd(np.stack(patches), pitch_x, pitch_y)
                result.update(
                    psd_od2_m2=psd.tolist(),
                    frequency_x_cycles_m=np.fft.fftfreq(shape[1], pitch_x).tolist(),
                    frequency_y_cycles_m=np.fft.fftfreq(shape[0], pitch_y).tolist(),
                )
            else:
                result.update(_unavailable("no complete valid uniform patch"))
        elif kind == "banding":
            phase, edges = array(request, "facet_phase"), array(request, "edge_positions_m")
            facets = _integer(request["facet_count"], "facet_count")
            if (
                phase.shape != (density.shape[0],)
                or edges.shape != phase.shape
                or not np.issubdtype(phase.dtype, np.integer)
                or facets < 2
                or np.any(phase >= facets)
            ):
                raise ValueError("banding phase and edges must be row vectors; phase is integer")
            row_count = mask.sum(axis=1)
            rows = row_count > 0
            row_levels = np.sum(np.where(mask, density, 0.0), axis=1)[rows] / row_count[rows]
            result = facet_banding(row_levels, phase[rows], edges[rows])
            groups = result["facet_level_offsets"]
            result["facet_count"] = facets
            result["periodic_components"] = None
            if all(str(group) in groups for group in range(facets)):
                offsets = np.array([groups[str(group)] for group in range(facets)])
                transform = np.fft.rfft(offsets - np.mean(offsets)) / facets
                factor = np.full(len(transform), 2.0)
                factor[0] = 1
                if facets % 2 == 0:
                    factor[-1] = 1
                result["periodic_components"] = [
                    {
                        "harmonic_order": index,
                        "amplitude_od": float(factor[index] * abs(value)),
                        "mean_square_od2": float(factor[index] * abs(value) ** 2),
                    }
                    for index, value in enumerate(transform)
                ]
            else:
                result["periodic_status"] = "not_computed: missing facet groups"
            mask &= ((phase >= 0) & np.isfinite(edges))[:, None]
            if not np.any(mask):
                result.update(_unavailable("no locked finite facet observations"))
        elif kind == "geometry":
            pixels = array(request, "point_pixels")
            actual, target = array(request, "actual_xy_m"), array(request, "target_xy_m")
            row_y = array(request, "row_y_m")
            if (
                pixels.ndim != 2
                or pixels.shape[1] != 2
                or not np.issubdtype(pixels.dtype, np.integer)
                or actual.shape != pixels.shape
                or target.shape != pixels.shape
                or row_y.shape != (density.shape[0],)
            ):
                raise ValueError("geometry needs paired points, integer [row,col] pixels and row y")
            if (
                np.any(pixels < 0)
                or np.any(pixels[:, 0] >= density.shape[0])
                or np.any(pixels[:, 1] >= density.shape[1])
            ):
                raise ValueError("geometry point pixel lies outside image")
            selected = mask[pixels[:, 0], pixels[:, 1]]
            selected &= np.all(np.isfinite(actual) & np.isfinite(target), axis=1)
            rows = np.any(mask, axis=1)
            used = np.zeros(density.shape, bool)
            used[pixels[selected, 0], pixels[selected, 1]] = True
            mask = used
            try:
                result = geometry_error(actual[selected], target[selected], row_y[rows])
            except ValueError as exc:
                result = _unavailable(str(exc))
            result["point_count"] = int(selected.sum())
            result["row_indices"] = np.flatnonzero(rows).tolist()
        elif kind == "step":
            row, start, stop, edge = [
                _integer(request[k], k) for k in ("row", "start_col", "stop_col", "edge_col")
            ]
            period = _number(request["sample_period_s"], "sample_period_s", positive=True)
            tolerance = _number(request.get("tolerance_fraction", 0.02), "tolerance_fraction")
            if not 0 <= row < density.shape[0] or not 0 <= start < edge < stop <= density.shape[1]:
                raise ValueError("step bounds must be inside the image and contain the edge")
            used = np.zeros(density.shape, bool)
            used[row, start:stop] = mask[row, start:stop]
            mask = used
            if not np.all(mask[row, start:stop]):
                result = _unavailable("step interval includes invalid pixels")
            else:
                try:
                    result = step_recovery(density[row, start:stop], edge - start, tolerance)
                    count = result["settling_samples"]
                    result.update(
                        settling_s=count * period if count is not None else None,
                        sample_period_s=period,
                        domain="density",
                    )
                except ValueError as exc:
                    result = _unavailable(str(exc))
        else:
            raise ValueError(f"unsupported quality metric kind: {kind}")
        result.setdefault("status", "computed" if np.any(mask) else "not_computed")
        result.update(kind=kind, evaluation_pixels=int(mask.sum()))
        results[name] = result
        masks[f"target_{name}"] = mask
    return results, masks, inputs
