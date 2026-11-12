"""Compare observed tables on the same preserved control references and held-out ADC files."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

import numpy as np
from scipy.stats import t

from zlscan.calib.monotonic import UnidentifiableTransfer, fit_monotonic_transfer
from zlscan.calib.radiometric import reference_observations
from zlscan.contracts.calibration import Binding, CalibItem, CalibrationHeader
from zlscan.experiment.grid_statistics import summarize, summarize_fraction
from zlscan.experiment.low_light import digest_json, evaluate_codes, read_capture
from zlscan.io.manifest import sha256_file


def analyze(source: Path, output: Path, *, verify=False):
    original_path = source / "report.json"
    original = json.loads(original_path.read_text())
    spec = original["specification"]
    observed, protocol, points = {}, {}, {}
    for raw in original["raw_inputs"]:
        path = source / raw["path"]
        if sha256_file(path) != raw["sha256"]:
            raise ValueError("control input hash changed")
        key = raw["case"], raw["replicate"], raw["role"]
        observed[key], protocol[key] = read_capture(path)
        if raw["role"] == "reference":
            arrays = observed[key]
            wedge = np.concatenate(
                [arrays[f"wedge_{i}"] for i in range(len(spec["reference_density"]))], axis=1
            )
            points[key[:2]] = reference_observations(
                arrays["dark"], arrays["white"], wedge, spec["reference_density"]
            )[3]
    signal = np.r_[0.0, 1.0, 10 ** -np.asarray(spec["reference_density"])]
    records = []
    for baseline in original["records"]:
        case, rep = baseline["case"], baseline["replicate"]
        centre = points[case, rep]
        others = [v for (name, index), v in points.items() if name == case and index != rep]
        if baseline["shot_noise"]:
            if len(others) < 2:
                raise ValueError(
                    "independent reference replicates are insufficient for uncertainty"
                )
            # One capture's statistic uses dispersion from independent other captures, not SEM.
            critical = t.ppf(1 - 0.05 / (2 * len(signal)), len(others) - 1)
            half = np.maximum(0.5, critical * np.std(others, axis=0, ddof=1))
            uncertainty = (
                "ASSUMED normal approximation for each reference statistic; dispersion from "
                "independent other reference captures, not SEM; Bonferroni t multiplier; "
                "minimum +/-0.5 code for quantization. Not per-sample noise or pixel confidence."
            )
        else:
            half = np.full(len(signal), 0.5)
            uncertainty = (
                "ASSUMED deterministic ADC rounding +/-0.5 code; exact synthetic target ratios"
            )
        binding = Binding(**protocol[case, rep, "validation"]["study_binding"])
        try:
            model = fit_monotonic_transfer(
                centre,
                signal,
                code_half_width=half,
                signal_bounds=np.column_stack([signal, signal]),
                lut_points=257,
            )
        except UnidentifiableTransfer as exc:
            records.append(
                {
                    "case": case,
                    "replicate": rep,
                    "status": "unidentifiable",
                    "reason": str(exc),
                    "uncertainty_source": uncertainty,
                    "code_half_width": half.tolist(),
                    "diagnostics": exc.diagnostics,
                    "observed_validation": [
                        {
                            "bias_od_valid_only": None,
                            "invalid_fraction": 1.0,
                            "status": "calibration_unidentifiable",
                        }
                        for _ in spec["held_out_density"]
                    ],
                    "log_diagnostic_validation": baseline["diagnostic_candidate_validation"],
                }
            )
            continue
        transfer = {
            **asdict(model),
            "uncertainty_source": uncertainty,
            "reference_assessment": {
                "status": "not_assessed",
                "scope": "held-out validation required",
            },
        }
        refs = [
            r["sha256"]
            for r in original["raw_inputs"]
            if r["case"] == case and r["role"] == "reference"
        ]
        calibration = CalibrationHeader(
            format_version=2,
            provenance="SIMULATED",
            calibration_id=digest_json({"transfer": transfer, "reference_inputs": refs}),
            created_at="assumed-study",
            binds_to=binding,
            valid_conditions={
                "radiometry": "global static table; no spatial flat field or dynamic inverse"
            },
            generator={"name": "observed_monotonic", "version": 1, "reference_raw_sha256": refs},
            items={
                "transfer_lut": CalibItem(
                    value={"channels": {"0": transfer}, "extrapolation": "invalid"}
                )
            },
        )
        encoded = calibration.model_dump_json(indent=2)
        target = output / f"{case}_r{rep}_monotonic.json"
        if verify:
            assert target.read_text() == encoded
        else:
            with target.open("x") as stream:
                stream.write(encoded)
        arrays = observed[case, rep, "validation"]
        validation = [
            evaluate_codes(
                arrays[f"validation_{i}"],
                calibration,
                binding,
                expected_density=d,
                pitch_m=spec["pitch_m"],
                bits=spec["bits"],
            )
            for i, d in enumerate(spec["held_out_density"])
        ]
        records.append(
            {
                "case": case,
                "replicate": rep,
                "status": "supported",
                "uncertainty_source": uncertainty,
                "code_half_width": half.tolist(),
                "valid_code_ranges": model.valid_code_ranges,
                "excluded_code_ranges": model.excluded_code_ranges,
                "unidentifiable_edges": model.unidentifiable_edges,
                "calibration_sha256": sha256_file(target),
                "observed_validation": validation,
                "log_diagnostic_validation": baseline["diagnostic_candidate_validation"],
            }
        )
    summaries = []
    for case in spec["cases"]:
        rows = [r for r in records if r["case"] == case["name"]]
        for method, key in (
            ("log_diagnostic_candidate", "log_diagnostic_validation"),
            ("observed_monotonic", "observed_validation"),
        ):
            summaries.append(
                {
                    "case": case["name"],
                    "method": method,
                    "held_out": [
                        {
                            "expected_density": d,
                            "bias_od_valid_only": summarize(
                                [r[key][i]["bias_od_valid_only"] for r in rows]
                            ),
                            "invalid_fraction": summarize_fraction(
                                [r[key][i]["invalid_fraction"] for r in rows]
                            ),
                        }
                        for i, d in enumerate(spec["held_out_density"])
                    ],
                }
            )
    unchanged = all(sha256_file(source / r["path"]) == r["sha256"] for r in original["raw_inputs"])
    assert unchanged
    report = {
        "source_directory": str(source),
        "source_report_sha256": sha256_file(original_path),
        "source_raw_inputs": [
            {k: r[k] for k in ("path", "sha256", "case", "role", "replicate")}
            for r in original["raw_inputs"]
        ],
        "evidence": "ASSUMED research protocol",
        "records": records,
        "summaries": summaries,
        "source_inputs_unchanged": unchanged,
        "high_density_capability": "not_validated",
        "scope": "Same reference statistics and held-out data as prior controls. "
        "Only references estimate uncertainty; validation labels enter after reconstruction. "
        "Reference uncertainty uses a declared normal approximation, not pixel confidence. "
        "Log results are prior diagnostic candidates. No model selection or device certification.",
    }
    # Normalize tuples for exact fresh-process JSON equality.
    return json.loads(json.dumps(report, allow_nan=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if args.verify:
        expected = json.loads((args.verify / "report.json").read_text())
        actual = analyze(Path(expected["source_directory"]), args.verify, verify=True)
        assert actual == expected
        print("fresh raw-only observed tables and independent reconstructions match")
    else:
        args.out.mkdir(parents=True, exist_ok=False)
        report = analyze(args.source, args.out)
        (args.out / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
        print(f"{len(report['records'])} observed-table cases accounted; source data unchanged")
