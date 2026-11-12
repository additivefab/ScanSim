"""Run the fixed Stage 4 factorial experiment on observable captures."""

from __future__ import annotations

import argparse
import itertools
import json
from dataclasses import replace
from pathlib import Path

import numpy as np

from zlscan.calib.file import save_calibration
from zlscan.calib.observations import calibration_from_scan
from zlscan.config.presets import nominal_config
from zlscan.contracts.calibration import CaptureDescription
from zlscan.engine.physical_run import run_physical
from zlscan.experiment.calibration_scan import run_calibration_scan
from zlscan.metrics.quality import compare_density
from zlscan.recon.capture import reconstruct_capture
from zlscan.recon.density import Grid
from zlscan.truth.psf import PsfConfig, effective_transmittance
from zlscan.truth.scene import step_wedge

EFFECTS = ("noise", "spot", "bandwidth")
LINES = 12


def _run_case(
    path: Path, active: tuple[str, ...]
) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict]:
    cfg = nominal_config()
    cfg.detector = replace(cfg.detector, shot_noise="noise" in active)
    if "spot" not in active:
        cfg.psf = PsfConfig(0, 0)
    if "bandwidth" not in active:
        cfg.afe = replace(cfg.afe, pole_hz=0, aa_poles_hz=())
    grid = Grid(cfg.scan.nx, LINES, cfg.scan.pitch_x_m, cfg.scan.pitch_y_m)
    width, height = grid.width * grid.pitch_x_m, grid.height * grid.pitch_y_m
    scene = step_wedge([0.25, 0.95, 1.65], width, height)
    cfg.scenes = {"main": scene}

    reference = path / "reference"
    run_calibration_scan(reference, cfg, lines_per_target=4)
    observed = reference / "observable"
    cal = calibration_from_scan(
        observed / "raw.zlraw", observed / "scan_protocol.json", fit_transfer=True
    )
    save_calibration(path / "calibration.json", cal)
    sample = path / "sample"
    run_physical(sample, cfg, LINES)
    observed = sample / "observable"
    capture = CaptureDescription.model_validate_json((observed / "capture.json").read_bytes())
    manifest = reconstruct_capture(
        observed / "raw.zlraw", capture, cal, grid, path / "reconstructed"
    )
    with np.load(path / "reconstructed" / "density.npz") as image:
        density, flags = image["density"], image["flags"]
    x, y = np.meshgrid(
        (np.arange(grid.width) + 0.5) * grid.pitch_x_m,
        (np.arange(grid.height) + 0.5) * grid.pitch_y_m,
    )
    optical = -np.log10(effective_transmittance(scene.lookup, cfg.psf, x.ravel(), y.ravel()))
    return density, flags, optical.reshape(x.shape), manifest


def factorial_interactions(mse: dict[tuple[str, ...], float]) -> dict[str, float]:
    """Subtract baseline and lower-order terms from each factorial MSE."""
    effects = {}
    for subset in sorted(mse, key=len):
        terms = [value for lower, value in effects.items() if set(lower) < set(subset)]
        effects[subset] = mse[subset] - sum(terms)
    return {"+".join(subset) or "baseline": value for subset, value in effects.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    out = args.output
    out.mkdir(parents=True)
    cases = {}
    for size in range(len(EFFECTS) + 1):
        for active in itertools.combinations(EFFECTS, size):
            name = "+".join(active) or "ideal"
            print(f"Running {name}", flush=True)
            cases[active] = _run_case(out / name, active)

    unblurred = cases[()][2]
    region = np.zeros(unblurred.shape, dtype=bool)
    region[2:-2, 2:-2] = True
    common = region.copy()
    for density, flags, optical, _ in cases.values():
        common &= (flags == 0) & np.isfinite(density) & np.isfinite(optical)
    if not np.any(common):
        raise ValueError("no common valid pixels in the fixed region")
    results = {}
    mse = {}
    for active, (density, flags, optical, manifest) in cases.items():
        name = "+".join(active) or "ideal"
        comparison = compare_density(
            density, flags, {"unblurred": unblurred, "optical": optical}, common
        )
        mse[active] = comparison.metrics["references"]["unblurred"]["mse"]
        results[name] = {
            "active_effects": list(active),
            "calibration_id": manifest["calibration_id"],
            "full_frame_valid_fraction": manifest["valid_fraction"],
            "comparison": comparison.metrics,
        }
    summary = {
        "mode": "stage4_factorial",
        "contains_truth": True,
        "configuration": "ASSUMED nominal single-channel",
        "optical_reference": "spot-integrated transmittance at output grid centres",
        "calibration_policy": "separate observable calibration for each condition",
        "region_pixels": int(np.count_nonzero(region)),
        "common_pixels": int(np.count_nonzero(common)),
        "cases": results,
        "unblurred_mse_interactions_od_squared": factorial_interactions(mse),
    }
    np.savez(out / "mask.npz", region=region, common=common)
    (out / "run.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
