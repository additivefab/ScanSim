"""Matched ASSUMED controls: log floor versus additive dark, with and without shot noise."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import replace
from pathlib import Path

from zlscan.calib.radiometric import LogTransferFit, assess_reference_fit
from zlscan.contracts.calibration import Binding
from zlscan.experiment.grid_statistics import summarize, summarize_fraction
from zlscan.experiment.low_light import (
    CapturePhase,
    evaluate_codes,
    fit_reference,
    fixture,
    read_capture,
    study_binding,
    write_capture,
)
from zlscan.io.manifest import sha256_file
from zlscan.truth.detector import E


def specification():
    return {
        "format": "zlscan-matched-calibration-controls/1",
        "evidence": "ASSUMED",
        "os": 32,
        "white_electron_rate_s": 1e8,
        "assumptions": {
            "excess_noise_factor": 1,
            "input_response_hz": 20_000,
            "post_log_poles_hz": [30_000, 50_000],
            "adc_range_v": [-1, 2.5],
        },
        "reference_density": [0.0, 1.0, 2.0, 3.0, 4.0],
        "held_out_density": [0.5, 1.5, 2.5, 3.5],
        "reference_rows": 16,
        "validation_rows": 16,
        "patch_columns": 64,
        "settle_samples": 512,
        "pitch_m": 0.001,
        "bits": 16,
        "noisy_replicates": 8,
        "deterministic_replicates": 1,
        "deterministic_reference_tolerance_code": 1.0,
        "cases": [
            {"name": "noiseless_floor_dark", "shot_noise": False, "dark_ratio": 0.0},
            {"name": "noiseless_additive_dark", "shot_noise": False, "dark_ratio": 0.001},
            {"name": "noisy_floor_dark", "shot_noise": True, "dark_ratio": 0.0},
            {"name": "noisy_additive_dark", "shot_noise": True, "dark_ratio": 0.001},
        ],
        "scope": "Input/post-log poles, ADC and fit fixed; only dark/noise controls vary. "
        "Noiseless code tolerance checks reference reproduction, not real measurement uncertainty. "
        "Seed sets are disjoint. No RMSE-subtraction attribution.",
    }


def audit(directory: Path):
    directory.mkdir(parents=True, exist_ok=False)
    spec = specification()
    (directory / "specification.json").write_text(json.dumps(spec, indent=2) + "\n")
    width = spec["patch_columns"] * len(spec["reference_density"])
    records, raw_inputs = [], []
    for ci, case in enumerate(spec["cases"]):
        reps = spec["noisy_replicates"] if case["shot_noise"] else spec["deterministic_replicates"]
        for rep in range(reps):
            captures = {}
            for role, base_seed in (("reference", 20_000), ("validation", 30_000)):
                cfg = fixture(spec["os"], base_seed + ci * 1000 + rep, spec["assumptions"])
                dark = case["dark_ratio"] * spec["white_electron_rate_s"] * E * cfg.detector.gain
                cfg.detector = replace(
                    cfg.detector, shot_noise=case["shot_noise"], dark_current_a=dark
                )
                if role == "reference":
                    phases = [
                        CapturePhase("dark", 0, (spec["reference_rows"], width)),
                        CapturePhase(
                            "white", spec["white_electron_rate_s"], (spec["reference_rows"], width)
                        ),
                    ]
                    phases += [
                        CapturePhase(
                            f"wedge_{i}",
                            spec["white_electron_rate_s"] * 10**-d,
                            (spec["reference_rows"], spec["patch_columns"]),
                        )
                        for i, d in enumerate(spec["reference_density"])
                    ]
                else:
                    phases = [
                        CapturePhase(
                            f"validation_{i}",
                            spec["white_electron_rate_s"] * 10**-d,
                            (spec["validation_rows"], width),
                        )
                        for i, d in enumerate(spec["held_out_density"])
                    ]
                path = directory / f"{case['name']}_r{rep}_{role}.zlraw"
                raw = write_capture(path, cfg, phases, settle_samples=spec["settle_samples"])
                raw_inputs.append({**raw, "case": case["name"], "role": role, "replicate": rep})
                captures[role] = read_capture(path)[0]
            binding = study_binding(cfg)
            candidate, fit_values = fit_reference(
                captures["reference"],
                spec["reference_density"],
                binding,
                f"{case['name']}_r{rep}_candidate",
            )
            candidate.generator["scope"] = (
                "matched-control candidate; reference acceptance not yet assessed"
            )
            candidate.items["transfer_lut"].value["channels"]["0"]["reference_assessment"] = (
                assess_reference_fit(LogTransferFit(**fit_values), tolerance_code=None)
            )
            stem = directory / f"{case['name']}_r{rep}"
            Path(str(stem) + "_candidate.json").write_text(candidate.model_dump_json(indent=2))
            # Diagnose independent data before declaring reference acceptance.
            results = [
                evaluate_codes(
                    captures["validation"][f"validation_{i}"],
                    candidate,
                    binding,
                    expected_density=d,
                    pitch_m=spec["pitch_m"],
                    bits=spec["bits"],
                )
                for i, d in enumerate(spec["held_out_density"])
            ]
            tolerance = (
                None if case["shot_noise"] else spec["deterministic_reference_tolerance_code"]
            )
            assessment = assess_reference_fit(
                LogTransferFit(**fit_values), tolerance_code=tolerance
            )
            assessed = candidate.model_copy(deep=True)
            assessed.calibration_id = f"{case['name']}_r{rep}_assessed"
            assessed.items["transfer_lut"].value["channels"]["0"]["reference_assessment"] = (
                assessment
            )
            Path(str(stem) + "_assessed.json").write_text(assessed.model_dump_json(indent=2))
            rejected = False
            try:
                evaluate_codes(
                    captures["validation"]["validation_0"],
                    assessed,
                    binding,
                    expected_density=spec["held_out_density"][0],
                    pitch_m=spec["pitch_m"],
                    bits=spec["bits"],
                )
            except ValueError as exc:
                if assessment["status"] != "mismatch" or "reference" not in str(exc):
                    raise
                rejected = True
            assert rejected == (assessment["status"] == "mismatch")
            records.append(
                {
                    "case": case["name"],
                    "replicate": rep,
                    "shot_noise": case["shot_noise"],
                    "dark_ratio_assumed": case["dark_ratio"],
                    "fit_rms_code": fit_values["residual_rms_code"],
                    "fitted_dark_ratio": fit_values["dark_ratio"],
                    "reference_codes": fit_values["reference_codes"],
                    "fitted_reference_codes": fit_values["fitted_reference_codes"],
                    "reference_residual_codes": fit_values["reference_residual_codes"],
                    "reference_assessment": assessment,
                    "failed_assessment_rejected": rejected,
                    "diagnostic_candidate_validation": results,
                }
            )
            print(f"completed {case['name']} r={rep}, reference={assessment['status']}", flush=True)
    assert len({r["seed"] for r in raw_inputs}) == len(raw_inputs)
    assert all(sha256_file(directory / r["path"]) == r["sha256"] for r in raw_inputs)
    summaries = []
    for case in spec["cases"]:
        runs = [r for r in records if r["case"] == case["name"]]
        summaries.append(
            {
                "case": case["name"],
                "fit_rms_code": summarize([r["fit_rms_code"] for r in runs]),
                "reference_assessment_counts": dict(
                    Counter(r["reference_assessment"]["status"] for r in runs)
                ),
                "held_out": [
                    {
                        "expected_density": d,
                        "diagnostic_bias_od_valid_only": summarize(
                            [
                                r["diagnostic_candidate_validation"][i]["bias_od_valid_only"]
                                for r in runs
                            ]
                        ),
                        "invalid_fraction": summarize_fraction(
                            [
                                r["diagnostic_candidate_validation"][i]["invalid_fraction"]
                                for r in runs
                            ]
                        ),
                    }
                    for i, d in enumerate(spec["held_out_density"])
                ],
            }
        )
    result = {
        "specification": spec,
        "raw_inputs": raw_inputs,
        "raw_inputs_unchanged": True,
        "records": records,
        "summaries": summaries,
        "interpretation": "Candidate residuals are diagnostics before acceptance. "
        "Failures are rejected. Noise cases lack a declared tolerance; remain unassessed. "
        "Conditional changes are not variance contributions or hardware validation.",
        "high_density_capability": "not_validated",
    }
    (directory / "report.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    return result


def verify(directory: Path):
    report = json.loads((directory / "report.json").read_text())
    spec = report["specification"]
    for record in report["records"]:
        case_name, rep = record["case"], record["replicate"]
        case = next(c for c in spec["cases"] if c["name"] == case_name)
        observed, protocol = {}, None
        for role in ("reference", "validation"):
            source = next(
                r
                for r in report["raw_inputs"]
                if r["case"] == case_name and r["replicate"] == rep and r["role"] == role
            )
            path = directory / source["path"]
            assert sha256_file(path) == source["sha256"]
            observed[role], protocol = read_capture(path)
        binding = Binding(**protocol["study_binding"])
        candidate, fit = fit_reference(
            observed["reference"],
            spec["reference_density"],
            binding,
            f"{case_name}_r{rep}_candidate",
        )
        candidate.generator["scope"] = (
            "matched-control candidate; reference acceptance not yet assessed"
        )
        candidate.items["transfer_lut"].value["channels"]["0"]["reference_assessment"] = (
            assess_reference_fit(LogTransferFit(**fit), tolerance_code=None)
        )
        stem = directory / f"{case_name}_r{rep}"
        assert (
            candidate.model_dump_json(indent=2) == Path(str(stem) + "_candidate.json").read_text()
        )
        assert fit["residual_rms_code"] == record["fit_rms_code"]
        assert fit["dark_ratio"] == record["fitted_dark_ratio"]
        for i, d in enumerate(spec["held_out_density"]):
            result = evaluate_codes(
                observed["validation"][f"validation_{i}"],
                candidate,
                binding,
                expected_density=d,
                pitch_m=spec["pitch_m"],
                bits=spec["bits"],
            )
            assert result == record["diagnostic_candidate_validation"][i]
        tolerance = None if case["shot_noise"] else spec["deterministic_reference_tolerance_code"]
        assessment = assess_reference_fit(LogTransferFit(**fit), tolerance_code=tolerance)
        assert assessment == record["reference_assessment"]
        assessed = candidate.model_copy(deep=True)
        assessed.calibration_id = f"{case_name}_r{rep}_assessed"
        assessed.items["transfer_lut"].value["channels"]["0"]["reference_assessment"] = assessment
        assert assessed.model_dump_json(indent=2) == Path(str(stem) + "_assessed.json").read_text()
    assert all(sha256_file(directory / r["path"]) == r["sha256"] for r in report["raw_inputs"])
    print(f"{len(report['records'])} raw-only control refits and independent reconstructions match")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if args.verify:
        verify(args.verify)
    else:
        audit(args.out)
