"""Actual radiometry CLI from independent synthetic linear-readout samples, with fresh refits.

This fixture is not a physical noise generator, spectrum or hardware measurement. Known
coefficients are used only by this evaluator; the fitter sees exported samples and controls.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

from zlscan.ident.radiometry import RadiometryFitConfig, identify_radiometry_files
from zlscan.io.manifest import sha256_file

MEAN = [1.3e-9, 1.8e-9, 2.1e-4, 3.2e-5]
FLOOR, SLOPE, N = 3e-20, 4e-12, 512
CASES = {
    "identified": (0, "identified", "compatible"),
    "uniform": (1, "partially_identifiable", "compatible"),
    "no_dark": (1, "partially_identifiable", "compatible"),
    "correlated": (1, "rejected_assumptions", "not_executed"),
    "variance_mismatch": (1, "identified", "mismatch"),
    "multiwindow": (0, "identified", "compatible"),
    "validation_correlated": (1, "identified", "rejected_assumptions"),
}


def generate(root, case):
    root.mkdir()
    rng = np.random.default_rng(72601113)
    controls = [(False, 1e-4, 0.0, 0.0)] + [
        (True, p, t, s)
        for p in [1e-4, 2e-4]
        for t, s in [(0.0, 0.0), (0.2, 0.8), (0.8, 0.2), (1.0, 1.0)]
    ]
    rows = []
    for partition in ["fit", "validation"]:
        for index, (gate, power, target, surround) in enumerate(controls):
            if case == "no_dark" and not gate:
                continue
            if case == "uniform":
                surround = target
            mean = MEAN[0] + gate * (MEAN[1] + power * (MEAN[2] * target + MEAN[3] * surround))
            variance = FLOOR + SLOPE * mean
            standard = np.sqrt(variance)
            y = rng.normal(0, standard, N)
            if case == "correlated" or (
                case == "validation_correlated" and partition == "validation"
            ):
                for k in range(1, N):
                    y[k] = 0.92 * y[k - 1] + 0.3 * y[k]
            if case == "variance_mismatch" and partition == "validation":
                y *= 3
            y += mean
            name = f"{partition}-{index}"
            path = root / (name + ".csv")
            np.savetxt(
                path,
                np.column_stack([np.arange(N) * 0.0005, y]),
                delimiter=",",
                header="time,response",
                comments="",
            )
            doc = dict(
                time_column="time",
                time_unit="s",
                source_kind="synthetic",
                device="synthetic linear readout",
                sample_interval_s=0.0005,
                time_tolerance_s=1e-12,
                trigger_source="synthetic capture",
                trigger_threshold_v=0.0,
                time_origin="capture start",
                session=name,
                epoch=0,
                simultaneous=True,
                channels=[
                    dict(
                        column="response",
                        unit="A",
                        measurement_point="synthetic pre-log current",
                        probe_ratio=1.0,
                        coupling="DC",
                        bandwidth_limit_hz=2000.0,
                        probe_compensated=True,
                        acquisition_id=name,
                    )
                ],
            )
            meta = root / (name + ".json")
            meta.write_text(json.dumps(doc), encoding="utf-8")
            rows.append(
                dict(
                    name=name,
                    csv=path.name,
                    metadata=meta.name,
                    source_sha256=sha256_file(path),
                    metadata_sha256=sha256_file(meta),
                    partition=partition,
                    channel="response",
                    window_s=[0.0, (N - 1) * 0.0005],
                    light_on=gate,
                    incident_power_w=power,
                    target_transmittance=target,
                    surround_transmittance=surround,
                    mean_standard_uncertainty_si=float(standard / np.sqrt(N)),
                    uncertainty_source="explicit known synthetic mean sigma",
                    controls_source="synthetic independently selected center/surround and shutter",
                    quality_source="synthetic settled unclipped linear readout",
                    instrument_settings_ref="synthetic fixed gain",
                )
            )
    if case == "multiwindow":
        fit = [r for r in rows if r["partition"] == "fit"]
        y = np.concatenate(
            [np.loadtxt(root / r["csv"], delimiter=",", skiprows=1)[:, 1] for r in fit]
        )
        path = root / "combined.csv"
        np.savetxt(
            path,
            np.column_stack([np.arange(len(y)) * 0.0005, y]),
            delimiter=",",
            header="time,response",
            comments="",
        )
        meta = root / fit[0]["metadata"]
        for i, r in enumerate(fit):
            r.update(
                csv=path.name,
                source_sha256=sha256_file(path),
                metadata=meta.name,
                metadata_sha256=sha256_file(meta),
                window_s=[i * N * 0.0005, ((i + 1) * N - 1) * 0.0005],
            )
    config = dict(
        format="zlscan.radiometry-fit-config/1",
        response_unit="A",
        linear_response_ref="synthetic linear pre-log measurement",
        instrument_settings_ref="synthetic fixed gain",
        surround_definition_ref="explicit synthetic broad-field average",
        assumption_source="independent Gaussian exported samples, not a physical spectral model",
        confidence_level=0.95,
        rank_rtol=1e-10,
        input_handling="conditional_on_declared_optical_controls",
        noise_model="iid_gaussian_linear_variance_in_mean_response",
        max_abs_lag1_correlation=0.25,
        max_half_mean_shift_sigma=6.0,
        validation_max_abs_mean_residual_si=3e-10,
        validation_max_variance_ratio=1.6,
        experiments=rows,
    )
    (root / "fit.json").write_text(json.dumps(config), encoding="utf-8")


def verify(root):
    errors = []
    for case, (_, status, validation) in CASES.items():
        folder = root / case
        path = folder / "fit.json"
        config = RadiometryFitConfig.model_validate_json(path.read_bytes())
        actual = identify_radiometry_files(config, folder)
        actual["config_sha256"] = sha256_file(path)
        delivered = json.loads((folder / "result/run.json").read_bytes())
        assert json.loads(json.dumps(actual)) == delivered
        assert delivered["status"] == status and delivered["validation"]["status"] == validation, (
            case,
            delivered["status"],
            delivered["validation"]["status"],
        )
        assert not delivered["parameter_upgrades"] and not delivered["hard_acceptance_eligible"]
        for suffix in ["svg", "png"]:
            assert (folder / "result" / ("radiometry_residuals." + suffix)).stat().st_size > 1000
        if case == "identified":
            mean = delivered["mean_fit"]
            for name, truth in zip(mean["coefficient_order"], MEAN, strict=True):
                p = mean["parameters"][name]
                error = abs(p["value"] - truth) / p["standard_uncertainty"]
                assert error < 4
                errors.append(error)
            noise = delivered["noise_fit"]
            truths = [FLOOR + SLOPE * noise["reference_mean_si"], SLOPE]
            for name, truth in zip(noise["coefficient_order"], truths, strict=True):
                p = noise["parameters"][name]
                error = abs(p["value"] - truth) / p["standard_uncertainty"]
                assert error < 4
                errors.append(error)
        if case == "correlated":
            assert delivered["mean_fit"] is None and delivered["noise_fit"] is None
            assert all(
                r["noise"]["variance_interval_si2"] is None for r in delivered["observations"]
            )
        if case == "uniform":
            assert delivered["glare_fraction"]["value"] is None
        if case == "validation_correlated":
            baseline = json.loads((root / "identified/result/run.json").read_bytes())
            assert delivered["mean_fit"] == baseline["mean_fit"]
            assert delivered["noise_fit"] == baseline["noise_fit"]
            assert all(
                r["noise"]["variance_interval_si2"] is None
                for r in delivered["observations"]
                if r["partition"] == "validation"
            )
        if case == "no_dark":
            assert delivered["mean_fit"]["parameters"]["lit_stray"]["value"] is None
        if case == "multiwindow":
            fit = [r for r in delivered["observations"] if r["partition"] == "fit"]
            assert len({r["source_sha256"] for r in fit}) == 1
            assert all(
                a["source_sample_range"][1] < b["source_sample_range"][0]
                for a, b in zip(fit[:-1], fit[1:], strict=True)
            )
    return dict(
        format="zlscan.radiometric-identification-audit/1",
        cases=len(CASES),
        maximum_parameter_error_standard_uncertainties=max(errors),
        parameter_upgrades=[],
        actual_measurements_obtained=False,
        scope="conditional mean and approximate variance moments; not spectra or hardware",
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--verify", action="store_true")
    args = p.parse_args()
    root = args.out.resolve()
    if args.verify:
        assert verify(root) == json.loads((root / "audit.json").read_bytes())
        print("Fresh-process radiometric refit and readback passed")
        return
    root.mkdir(parents=True, exist_ok=False)
    (root / "assumptions.json").write_text(
        json.dumps(
            dict(
                mean_coefficients=MEAN,
                variance_floor_si2=FLOOR,
                variance_slope_si=SLOPE,
                samples_per_window=N,
                evaluator_limit_standard_uncertainties=4,
                actual_measurements_obtained=False,
                physical_noise_simulation=False,
            )
        ),
        encoding="utf-8",
    )
    for case, (expected, _, _) in CASES.items():
        folder = root / case
        generate(folder, case)
        proc = subprocess.run(
            [
                sys.executable,
                "-I",
                "-X",
                "utf8",
                "-m",
                "zlscan",
                "ident",
                "--radiometry-fit",
                str(folder / "fit.json"),
                "--out",
                str(folder / "result"),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        (folder / "stdout.txt").write_text(proc.stdout, encoding="utf-8")
        (folder / "stderr.txt").write_text(proc.stderr, encoding="utf-8")
        assert proc.returncode == expected, (case, proc.stderr)
    (root / "audit.json").write_text(json.dumps(verify(root), indent=2) + "\n", encoding="utf-8")
    print("Public radiometric identification audit passed")


if __name__ == "__main__":
    main()
