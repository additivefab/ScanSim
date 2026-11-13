"""Independent illumination controls distinguish additive terms; Gaussian noise is conditional."""

import json

import numpy as np
import pytest

from zlscan.ident.radiometry import RadiometryFitConfig, identify_radiometry_files
from zlscan.io.manifest import sha256_file

MEAN = [1e-9, 2e-9, 2e-4, 3e-5]


def fixture(root, *, uniform=False, no_dark=False, correlated=False, voltage=False):
    rng = np.random.default_rng(20261113)
    rows = []
    # Independent fit/holdout acquisitions; inputs deliberately span the same support.
    controls = [(False, 1e-4, 0.0, 0.0)] + [
        (True, p, t, s)
        for p in [1e-4, 2e-4]
        for t, s in [(0.0, 0.0), (0.2, 0.8), (0.8, 0.2), (1.0, 1.0)]
    ]
    for part in ["fit", "validation"]:
        for index, (gate, power, target, surround) in enumerate(controls):
            if no_dark and not gate:
                continue
            if uniform:
                surround = target
            mean = MEAN[0] + gate * (MEAN[1] + power * (MEAN[2] * target + MEAN[3] * surround))
            variance = 1e-20 + 5e-12 * mean
            samples = rng.normal(mean, np.sqrt(variance), 2048)
            if correlated:
                for k in range(1, len(samples)):
                    samples[k] = mean + 0.95 * (samples[k - 1] - mean) + 0.1 * (samples[k] - mean)
            name = f"{part}-{index}"
            csv = root / (name + ".csv")
            np.savetxt(
                csv,
                np.column_stack([np.arange(len(samples)) * 1e-3, samples]),
                delimiter=",",
                header="time,response",
                comments="",
            )
            metadata = dict(
                time_column="time",
                time_unit="s",
                source_kind="synthetic",
                device="synthetic linear detector",
                sample_interval_s=0.001,
                time_tolerance_s=1e-12,
                trigger_source="synthetic record",
                trigger_threshold_v=0.0,
                time_origin="record start",
                session=name,
                epoch=0,
                simultaneous=True,
                channels=[
                    dict(
                        column="response",
                        unit="V" if voltage else "A",
                        measurement_point="linear output",
                        probe_ratio=1.0,
                        coupling="DC",
                        bandwidth_limit_hz=100.0,
                        probe_compensated=True,
                        acquisition_id=name,
                    )
                ],
            )
            meta = root / (name + ".json")
            meta.write_text(json.dumps(metadata), encoding="utf-8")
            rows.append(
                dict(
                    name=name,
                    csv=csv.name,
                    metadata=meta.name,
                    source_sha256=sha256_file(csv),
                    metadata_sha256=sha256_file(meta),
                    partition=part,
                    channel="response",
                    window_s=[0.0, 2.047],
                    light_on=gate,
                    incident_power_w=power,
                    target_transmittance=target,
                    surround_transmittance=surround,
                    mean_standard_uncertainty_si=float(np.sqrt(variance / len(samples))),
                    uncertainty_source="known synthetic mean error, independent Gaussian samples",
                    controls_source="explicit synthetic aperture/illumination protocol",
                    quality_source="declared settled linear unclipped interval",
                    instrument_settings_ref="synthetic fixed gain",
                )
            )
    return dict(
        format="zlscan.radiometry-fit-config/1",
        response_unit="V" if voltage else "A",
        linear_response_ref="synthetic pre-log linear readout",
        instrument_settings_ref="synthetic fixed gain",
        surround_definition_ref="synthetic known broad-area average",
        assumption_source="conditional synthetic protocol",
        confidence_level=0.95,
        rank_rtol=1e-10,
        input_handling="conditional_on_declared_optical_controls",
        noise_model="iid_gaussian_linear_variance_in_mean_response",
        max_abs_lag1_correlation=0.2,
        max_half_mean_shift_sigma=5.0,
        validation_max_abs_mean_residual_si=2e-10,
        validation_max_variance_ratio=1.4,
        experiments=rows,
    )


def run(root, config):
    return identify_radiometry_files(
        RadiometryFitConfig.model_validate_json(json.dumps(config)), root
    )


def test_distinguishes_stray_baseline_direct_and_glare_and_noise(tmp_path):
    result = run(tmp_path, fixture(tmp_path))
    assert result["status"] == "identified"
    assert result["validation"]["status"] == "compatible"
    mean = result["mean_fit"]
    for name, expected in zip(mean["coefficient_order"], MEAN, strict=True):
        p = mean["parameters"][name]
        assert abs(p["value"] - expected) < 4 * p["standard_uncertainty"]
    assert result["noise_fit"]["rank"] == 2
    assert result["glare_fraction"]["value"] == pytest.approx(MEAN[3] / sum(MEAN[2:]), abs=0.003)
    assert result["parameter_upgrades"] == [] and result["hard_acceptance_eligible"] is False
    assert all(r["noise"]["variance_interval_si2"] is not None for r in result["observations"])


@pytest.mark.parametrize("kind", ["uniform", "no_dark"])
def test_degenerate_controls_preserve_only_supported_combinations(tmp_path, kind):
    result = run(tmp_path, fixture(tmp_path, uniform=kind == "uniform", no_dark=kind == "no_dark"))
    assert result["status"] == "partially_identifiable"
    parameters = result["mean_fit"]["parameters"]
    if kind == "uniform":
        assert parameters["direct_gain"]["value"] is None
        assert parameters["glare_gain"]["value"] is None
        assert result["glare_fraction"]["value"] is None
    else:
        assert parameters["baseline"]["value"] is None
        assert parameters["lit_stray"]["value"] is None


def test_correlated_records_reject_iid_inference_without_hiding_data(tmp_path):
    result = run(tmp_path, fixture(tmp_path, correlated=True))
    assert result["status"] == "rejected_assumptions"
    assert result["mean_fit"] is None and result["noise_fit"] is None
    assert result["observations"]
    assert any(r["noise"]["iid_diagnostic"] == "failed" for r in result["observations"])
    assert all(r["noise"]["variance_interval_si2"] is None for r in result["observations"])


def test_validation_does_not_refit(tmp_path):
    config = fixture(tmp_path)
    before = run(tmp_path, config)
    for row in config["experiments"]:
        if row["partition"] != "validation":
            continue
        path = tmp_path / row["csv"]
        data = np.loadtxt(path, delimiter=",", skiprows=1)
        data[:, 1] += 1e-8
        np.savetxt(path, data, delimiter=",", header="time,response", comments="")
        row["source_sha256"] = sha256_file(path)
    after = run(tmp_path, config)
    assert after["validation"]["status"] == "mismatch"
    assert before["mean_fit"] == after["mean_fit"]
    assert before["noise_fit"] == after["noise_fit"]


def test_source_reuse_and_metadata_tamper_rejected(tmp_path):
    config = fixture(tmp_path)
    config["experiments"][-1].update(
        csv=config["experiments"][0]["csv"], source_sha256=config["experiments"][0]["source_sha256"]
    )
    with pytest.raises(ValueError, match="reuse"):
        run(tmp_path, config)


def test_linear_voltage_is_allowed_but_ac_is_rejected(tmp_path):
    config = fixture(tmp_path, voltage=True)
    assert run(tmp_path, config)["response_unit"] == "V"
    row = config["experiments"][0]
    path = tmp_path / row["metadata"]
    doc = json.loads(path.read_bytes())
    doc["channels"][0]["coupling"] = "AC"
    path.write_text(json.dumps(doc), encoding="utf-8")
    row["metadata_sha256"] = sha256_file(path)
    with pytest.raises(ValueError, match="DC"):
        run(tmp_path, config)


def combined_fit(root):
    config = fixture(root)
    fit = [r for r in config["experiments"] if r["partition"] == "fit"]
    arrays = [np.loadtxt(root / r["csv"], delimiter=",", skiprows=1) for r in fit]
    combined = np.vstack(arrays)
    combined[:, 0] = np.arange(len(combined)) * 0.001
    csv = root / "combined.csv"
    np.savetxt(csv, combined, delimiter=",", header="time,response", comments="")
    meta = root / fit[0]["metadata"]
    for index, row in enumerate(fit):
        row.update(
            csv=csv.name,
            source_sha256=sha256_file(csv),
            metadata=meta.name,
            metadata_sha256=sha256_file(meta),
            window_s=[index * 2048 * 0.001, ((index + 1) * 2048 - 1) * 0.001],
        )
    return config


def test_nonoverlapping_levels_of_one_fit_capture_are_allowed(tmp_path):
    result = run(tmp_path, combined_fit(tmp_path))
    assert result["status"] == "identified"
    rows = [r for r in result["observations"] if r["partition"] == "fit"]
    assert len({r["source_sha256"] for r in rows}) == 1
    assert all(
        a["source_sample_range"][1] < b["source_sample_range"][0]
        for a, b in zip(rows[:-1], rows[1:], strict=True)
    )


def test_overlap_within_same_partition_is_rejected(tmp_path):
    config = combined_fit(tmp_path)
    config["experiments"][1]["window_s"] = config["experiments"][0]["window_s"]
    with pytest.raises(ValueError, match="reuse"):
        run(tmp_path, config)


@pytest.mark.parametrize(
    "failure", ["metadata_hash", "settings", "window", "bandwidth", "log_codes"]
)
def test_invalid_metadata_controls_and_domain_are_rejected(tmp_path, failure):
    config = fixture(tmp_path)
    row = config["experiments"][0]
    if failure == "metadata_hash":
        row["metadata_sha256"] = "0" * 64
    elif failure == "settings":
        row["instrument_settings_ref"] = "changed gain"
    elif failure == "window":
        row["window_s"][1] = 100.0
    else:
        path = tmp_path / row["metadata"]
        doc = json.loads(path.read_bytes())
        if failure == "bandwidth":
            doc["channels"][0]["bandwidth_limit_hz"] = 17.0
        else:
            doc["channels"][0]["unit"] = "code"
        path.write_text(json.dumps(doc), encoding="utf-8")
        row["metadata_sha256"] = sha256_file(path)
    with pytest.raises(ValueError):
        run(tmp_path, config)


def test_validation_extrapolation_is_not_silently_used(tmp_path):
    config = fixture(tmp_path)
    row = next(
        r
        for r in config["experiments"]
        if r["partition"] == "validation" and r["target_transmittance"] > 0
    )
    row["incident_power_w"] = 0.1
    result = run(tmp_path, config)
    assert result["validation"]["status"] == "unidentifiable"
    assert any(
        r["invalid_reason"] == "outside_fit_design_hull" for r in result["validation"]["rows"]
    )


def test_uniform_holdout_does_not_validate_separate_glare_gain(tmp_path):
    config = fixture(tmp_path)
    for row in config["experiments"]:
        if row["partition"] == "validation":
            row["surround_transmittance"] = row["target_transmittance"]
    result = run(tmp_path, config)
    assert result["validation"]["status"] == "incomplete_coverage"


def test_clock_tolerance_cannot_enlarge_analysis_windows(tmp_path):
    config = combined_fit(tmp_path)
    path = tmp_path / config["experiments"][0]["metadata"]
    doc = json.loads(path.read_bytes())
    doc["time_tolerance_s"] = 0.1
    path.write_text(json.dumps(doc), encoding="utf-8")
    for row in config["experiments"]:
        if row["metadata"] == path.name:
            row["metadata_sha256"] = sha256_file(path)
    result = run(tmp_path, config)
    assert all(r["sample_count"] == 2048 for r in result["observations"])


def test_conditional_solver_covariance_and_hull_are_independently_checked():
    from zlscan.ident.conditional_linear import ConditionalLinear

    x = np.array([[1.0, 0.0], [1.0, 1e-20], [1.0, 2e-20], [1.0, 3e-20]])
    sigma = np.array([1.0, 2.0, 1.0, 2.0]) * 1e-12
    expected = np.array([3e-9, 2e12])
    y = x @ expected
    fit = ConditionalLinear(x, y, sigma, ["offset", "slope"], ["A", "A/W"], 0.95, 1e-10)
    covariance = np.linalg.inv(x.T @ ((1 / sigma**2)[:, None] * x))
    np.testing.assert_allclose(fit.report["conditional_covariance"], covariance, rtol=1e-12)
    assert fit.report["parameters"]["slope"]["value"] == pytest.approx(expected[1], rel=1e-12)
    # A hull needs sum(weights)=1 even if the supplied design has no constant column.
    no_intercept = ConditionalLinear(
        [[2.0], [4.0]], [6.0, 12.0], [0.1, 0.1], ["slope"], ["1"], 0.95, 1e-10
    )
    assert no_intercept.predict([3.0])["value"] == pytest.approx(9.0)
    assert no_intercept.predict([1.0])["invalid_reason"] == "outside_fit_design_hull"


def test_validation_variance_mismatch_does_not_refit_noise(tmp_path):
    config = fixture(tmp_path)
    before = run(tmp_path, config)
    for row in config["experiments"]:
        if row["partition"] == "validation":
            path = tmp_path / row["csv"]
            data = np.loadtxt(path, delimiter=",", skiprows=1)
            data[:, 1] = data[:, 1].mean() + 3 * (data[:, 1] - data[:, 1].mean())
            np.savetxt(path, data, delimiter=",", header="time,response", comments="")
            row["source_sha256"] = sha256_file(path)
    after = run(tmp_path, config)
    assert after["noise_fit"] == before["noise_fit"]
    assert after["validation"]["status"] == "mismatch"
    assert all(r["variance_ratio"] > 5 for r in after["validation"]["rows"])


def test_cli_report_rejection_and_no_overwrite(tmp_path):
    from zlscan.cli import main

    config = fixture(tmp_path)
    path = tmp_path / "fit.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    out = tmp_path / "report"
    args = ["ident", "--radiometry-fit", str(path), "--out", str(out)]
    assert main(args) == 0
    before = (out / "run.json").read_bytes()
    assert json.loads(before)["config_sha256"] == sha256_file(path)
    assert (out / "radiometry_residuals.svg").stat().st_size > 1000
    assert (out / "radiometry_residuals.png").stat().st_size > 1000
    assert main(args) == 1 and (out / "run.json").read_bytes() == before
    config["experiments"][0]["metadata_sha256"] = "0" * 64
    path.write_text(json.dumps(config), encoding="utf-8")
    new = tmp_path / "bad"
    assert main(["ident", "--radiometry-fit", str(path), "--out", str(new)]) == 1
    assert not new.exists()


def test_failed_validation_noise_assumption_never_changes_fit(tmp_path):
    config = fixture(tmp_path)
    before = run(tmp_path, config)
    for row in config["experiments"]:
        if row["partition"] == "validation":
            path = tmp_path / row["csv"]
            data = np.loadtxt(path, delimiter=",", skiprows=1)
            mean = data[:, 1].mean()
            for i in range(1, len(data)):
                data[i, 1] = mean + 0.95 * (data[i - 1, 1] - mean) + 0.1 * (data[i, 1] - mean)
            np.savetxt(path, data, delimiter=",", header="time,response", comments="")
            row["source_sha256"] = sha256_file(path)
    after = run(tmp_path, config)
    assert before["mean_fit"] == after["mean_fit"]
    assert before["noise_fit"] == after["noise_fit"]
    assert after["validation"]["status"] == "rejected_assumptions"


@pytest.mark.parametrize("kind", ["negative_glare", "negative_variance_slope"])
def test_inadmissible_candidate_is_reported_without_clamping(tmp_path, kind):
    config = fixture(tmp_path)
    for row in config["experiments"]:
        path = tmp_path / row["csv"]
        data = np.loadtxt(path, delimiter=",", skiprows=1)
        mean = float(data[:, 1].mean())
        if kind == "negative_glare":
            data[:, 1] -= (
                row["light_on"]
                * 2
                * MEAN[3]
                * row["incident_power_w"]
                * row["surround_transmittance"]
            )
        else:
            target_variance = 5e-19 - 5e-12 * mean
            data[:, 1] = mean + (data[:, 1] - mean) * np.sqrt(
                target_variance / np.var(data[:, 1], ddof=1)
            )
        np.savetxt(path, data, delimiter=",", header="time,response", comments="")
        row["source_sha256"] = sha256_file(path)
    result = run(tmp_path, config)
    assert result["status"] != "identified"
    if kind == "negative_glare":
        assert result["mean_fit"]["parameters"]["glare_gain"]["value"] < 0
    else:
        assert result["noise_fit"]["parameters"]["variance_slope"]["value"] < 0
