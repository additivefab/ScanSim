"""ASSUMED low-light steps and held-out calibration across grids."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path

import numpy as np

from zlscan.contracts.calibration import Binding
from zlscan.experiment.grid_statistics import equivalence, summarize, summarize_fraction
from zlscan.experiment.low_light import (
    CapturePhase,
    evaluate_codes,
    fit_reference,
    fixture,
    read_capture,
    step_metrics,
    write_capture,
)
from zlscan.io.manifest import sha256_file


def specification(smoke=False):
    return {
        "format": "zlscan-low-light-study/1",
        "evidence": "ASSUMED",
        "scope": "Numerical candidate-model study, not measured performance or spatial calibration",
        "grids": [4, 8, 16, 32],
        "replicates": 2 if smoke else 8,
        "cycles": 4 if smoke else 64,
        "phase_samples": 256,
        "settle_samples": 512,
        "reference_rows": 4 if smoke else 16,
        "patch_columns": 8 if smoke else 64,
        "validation_rows": 4 if smoke else 16,
        "white_rate_s": 1e8,
        "step_high_rate_s": 1e6,
        "step_low_rate_s": 1e5,
        "reference_density": [0.0, 1.0, 2.0, 3.0, 4.0],
        "held_out_density": [0.5, 1.5, 2.5, 3.5],
        "analysis_bin_pitch_m": 1e-3,
        "fs_hz": 1_000_000,
        "bits": 16,
        "assumptions": {
            "excess_noise_factor": 1,
            "input_response_hz": 20_000,
            "post_log_poles_hz": [30_000, 50_000],
            "adc_range_v": [-1, 2.5],
        },
        "margins": {
            "voltage_mean_v": 0.005,
            "band_power_ratio": 1.25,
            "bias_difference_od": 0.02,
            "invalid_fraction_difference": 0.02,
        },
        "family_alpha": 0.05,
        "smoke_only": smoke,
        "binding_policy": "named physical study instrument; os/seed recorded separately; "
        "cannot match production run calibration binding",
        "band_power_scope": "Welch constant-detrended final half-phase windows; "
        "7.8125 kHz bins, summed through 50 kHz, excludes unresolved low-frequency power",
        "uncertainty_scope": "t intervals over independent runs, not correlated pixels; "
        "fixed-finest arm uses paired differences due to shared calibration fit; "
        "fractions use bounded Hoeffding intervals, including zero observed invalid counts",
        "remaining": [
            "measured PMT/input response",
            "full parameter-range validation",
            "source/channel noise separation",
            "real device density capability",
        ],
    }


def generate(directory: Path, spec):
    directory.mkdir(parents=True, exist_ok=False)
    captures = []
    width = spec["patch_columns"] * len(spec["reference_density"])
    for gi, os in enumerate(spec["grids"]):
        for rep in range(spec["replicates"]):
            for role, base_seed in (
                ("step", 10_000),
                ("reference", 20_000),
                ("validation", 30_000),
            ):
                seed = base_seed + gi * 1000 + rep
                cfg = fixture(os, seed, spec["assumptions"])
                if (cfg.fs_hz, cfg.adc.bits) != (spec["fs_hz"], spec["bits"]):
                    raise ValueError("fixture ADC clock/width differs from declared study")
                if role == "step":
                    phases = [
                        CapturePhase(f"{direction}_{i}", rate, (1, spec["phase_samples"]))
                        for i in range(spec["cycles"])
                        for direction, rate in (
                            ("down", spec["step_low_rate_s"]),
                            ("up", spec["step_high_rate_s"]),
                        )
                    ]
                elif role == "reference":
                    phases = [
                        CapturePhase("dark", 0, (spec["reference_rows"], width)),
                        CapturePhase(
                            "white", spec["white_rate_s"], (spec["reference_rows"], width)
                        ),
                    ]
                    phases += [
                        CapturePhase(
                            f"wedge_{i}",
                            spec["white_rate_s"] * 10**-d,
                            (spec["reference_rows"], spec["patch_columns"]),
                        )
                        for i, d in enumerate(spec["reference_density"])
                    ]
                else:
                    phases = [
                        CapturePhase(
                            f"validation_{i}",
                            spec["white_rate_s"] * 10**-d,
                            (spec["validation_rows"], width),
                        )
                        for i, d in enumerate(spec["held_out_density"])
                    ]
                path = directory / f"os{os}_r{rep}_{role}.zlraw"
                capture = write_capture(
                    path,
                    cfg,
                    phases,
                    settle_samples=0 if role == "step" else spec["settle_samples"],
                    prewarm_samples=spec["settle_samples"] if role == "step" else 0,
                    prewarm_rate_s=spec["step_high_rate_s"],
                )
                captures.append({**capture, "grid": os, "replicate": rep, "role": role})
            print(f"generated assumed os={os}, replicate={rep}", flush=True)
    manifest = {"specification": spec, "captures": captures}
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2, allow_nan=False) + "\n")


def analyze(directory: Path):
    manifest = json.loads((directory / "manifest.json").read_text())
    spec, captures = manifest["specification"], manifest["captures"]
    if len({c["seed"] for c in captures}) != len(captures):
        raise ValueError("step/reference/validation or grid noise seeds overlap")
    data, fits, fit_rows, step_rows = {}, {}, [], []
    for cap in captures:
        path = directory / cap["path"]
        if sha256_file(path) != cap["sha256"]:
            raise ValueError("raw study input changed")
        arrays, protocol = read_capture(path)
        if protocol["seed"] != cap["seed"] or protocol["numeric_grid_os"] != cap["grid"]:
            raise ValueError("study input identity differs")
        key = cap["grid"], cap["replicate"]
        data[*key, cap["role"]] = (arrays, protocol)
        if cap["role"] == "reference":
            try:
                calibration, fit = fit_reference(
                    arrays,
                    spec["reference_density"],
                    Binding(**protocol["study_binding"]),
                    f"os{key[0]}_r{key[1]}",
                )
                encoded = calibration.model_dump_json(indent=2).encode()
                target = directory / f"os{key[0]}_r{key[1]}_calibration.json"
                if target.exists():
                    assert target.read_bytes() == encoded, "calibration rerun changed"
                else:
                    target.write_bytes(encoded)
                fits[key] = calibration
                fit_rows.append(
                    {
                        "grid": key[0],
                        "replicate": key[1],
                        "status": "fit",
                        "fit_rms_code": fit["residual_rms_code"],
                        "calibration_sha256": sha256_file(target),
                    }
                )
            except ValueError as exc:
                fits[key] = None
                fit_rows.append(
                    {
                        "grid": key[0],
                        "replicate": key[1],
                        "status": "unidentifiable",
                        "reason": str(exc),
                    }
                )
        elif cap["role"] == "step":
            metrics, waves, spectra = step_metrics(
                arrays,
                cycles=spec["cycles"],
                phase_samples=spec["phase_samples"],
                fs_hz=spec["fs_hz"],
                adc_range_v=spec["assumptions"]["adc_range_v"],
                bits=spec["bits"],
            )
            step_rows.append(
                {
                    "grid": key[0],
                    "replicate": key[1],
                    "metrics": metrics,
                    "waves": waves,
                    "spectra": spectra,
                }
            )
    finest = max(spec["grids"])
    validation_rows = []
    for grid in spec["grids"]:
        for rep in range(spec["replicates"]):
            arrays, protocol = data[grid, rep, "validation"]
            for arm, calibration in (
                ("per_grid", fits[grid, rep]),
                ("fixed_finest", fits[finest, rep]),
            ):
                for i, density in enumerate(spec["held_out_density"]):
                    result = (
                        evaluate_codes(
                            arrays[f"validation_{i}"],
                            calibration,
                            Binding(**protocol["study_binding"]),
                            expected_density=density,
                            pitch_m=spec["analysis_bin_pitch_m"],
                            bits=spec["bits"],
                        )
                        if calibration
                        else {
                            "bias_od_valid_only": None,
                            "invalid_fraction": None,
                            "status": "calibration_unidentifiable",
                        }
                    )
                    validation_rows.append(
                        {
                            "grid": grid,
                            "replicate": rep,
                            "arm": arm,
                            "expected_density": density,
                            **result,
                        }
                    )
    comparisons = []
    metrics = list(step_rows[0]["metrics"])
    for grid in spec["grids"][:-1]:
        for metric in metrics:
            a = [r["metrics"][metric] for r in step_rows if r["grid"] == grid]
            b = [r["metrics"][metric] for r in step_rows if r["grid"] == finest]
            ratio = metric.endswith("variance_v2")
            margin = (
                math.log(spec["margins"]["band_power_ratio"])
                if ratio
                else spec["margins"]["voltage_mean_v"]
            )
            comparisons.append(
                {
                    "family": "step",
                    "grid": grid,
                    "reference_grid": finest,
                    "metric": metric,
                    **equivalence(
                        a,
                        b,
                        margin,
                        alpha=spec["family_alpha"] / (len(metrics) * 3),
                        log_ratio=ratio,
                    ),
                }
            )
        for arm in ("per_grid", "fixed_finest"):
            for density in spec["held_out_density"]:
                for metric, margin in (
                    ("bias_od_valid_only", spec["margins"]["bias_difference_od"]),
                    ("invalid_fraction", spec["margins"]["invalid_fraction_difference"]),
                ):

                    def selected(g, metric=metric, arm=arm, density=density):
                        return [
                            r[metric]
                            for r in validation_rows
                            if r["grid"] == g
                            and r["arm"] == arm
                            and r["expected_density"] == density
                        ]

                    comparisons.append(
                        {
                            "family": "calibration",
                            "grid": grid,
                            "reference_grid": finest,
                            "arm": arm,
                            "expected_density": density,
                            "metric": metric,
                            **equivalence(
                                selected(grid),
                                selected(finest),
                                margin,
                                alpha=spec["family_alpha"] / (3 * 2 * 4 * 2),
                                paired=arm == "fixed_finest",
                                bounded_fraction=metric == "invalid_fraction",
                            ),
                        }
                    )
    step_summaries, calibration_summaries = [], []
    for grid in spec["grids"]:
        runs = [r for r in step_rows if r["grid"] == grid]
        step_summaries.append(
            {
                "grid": grid,
                "metrics": {m: summarize([r["metrics"][m] for r in runs]) for m in metrics},
                "mean_waves": {
                    direction: np.mean([r["waves"][direction] for r in runs], axis=0).tolist()
                    for direction in ("down", "up")
                },
            }
        )
        for arm in ("per_grid", "fixed_finest"):
            for density in spec["held_out_density"]:
                runs = [
                    r
                    for r in validation_rows
                    if r["grid"] == grid and r["arm"] == arm and r["expected_density"] == density
                ]
                calibration_summaries.append(
                    {
                        "grid": grid,
                        "arm": arm,
                        "expected_density": density,
                        "bias_od_valid_only": summarize([r["bias_od_valid_only"] for r in runs]),
                        "invalid_fraction": summarize_fraction(
                            [r["invalid_fraction"] for r in runs]
                        ),
                    }
                )
    # Verification after all consumers: fitting/reconstruction must not modify observation inputs.
    unchanged = all(sha256_file(directory / c["path"]) == c["sha256"] for c in captures)
    assert unchanged
    return {
        "specification": spec,
        "raw_inputs": [
            {k: c[k] for k in ("path", "sha256", "seed", "grid", "role", "replicate")}
            for c in captures
        ],
        "raw_inputs_unchanged": unchanged,
        "disjoint_seeds": True,
        "fits": fit_rows,
        "step_replicates": step_rows,
        "validation_replicates": validation_rows,
        "step_summaries": step_summaries,
        "calibration_summaries": calibration_summaries,
        "comparisons": comparisons,
        "comparison_status_counts": dict(Counter(c["status"] for c in comparisons)),
        "high_density_capability": "not_validated",
        "interpretation": "Execution/CRC success is not statistical equivalence. "
        "Inconclusive intervals and invalid coverage remain explicit. "
        "Bias uses valid pixels only; full-field accuracy is not established.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--reanalyze", type=Path)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    if args.reanalyze:
        result = analyze(args.reanalyze)
        expected = json.loads((args.reanalyze / "report.json").read_text())
        assert result == expected, "fresh analysis differs from published result"
        print("fresh-process raw-only fit/reconstruction/statistical analysis matches report")
    else:
        generate(args.out, specification(args.smoke))
        result = analyze(args.out)
        (args.out / "report.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
        print(json.dumps(result["comparison_status_counts"]))
