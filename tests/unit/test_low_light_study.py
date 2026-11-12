"""The study preserves observed source indices, binding and invalid data through reconstruction."""

from dataclasses import replace

import numpy as np
import pytest

from zlscan.contracts.calibration import require_applicable
from zlscan.engine.physical_run import simulation_binding
from zlscan.experiment.low_light import (
    CapturePhase,
    evaluate_codes,
    fit_reference,
    fixture,
    read_capture,
    study_binding,
    write_capture,
)

ASSUMPTIONS = {
    "excess_noise_factor": 1,
    "input_response_hz": 20_000,
    "post_log_poles_hz": [30_000, 50_000],
    "adc_range_v": [-1, 2.5],
}


def synthetic_reference():
    densities = [0.0, 1.0, 2.0, 3.0, 4.0]

    def code(t):
        return np.rint(50000 + 4500 * np.log10(t + 1e-4)).astype(np.uint16)

    shape = (8, 40)
    data = {"dark": np.full(shape, code(0)), "white": np.full(shape, code(1))}
    for i, density in enumerate(densities):
        data[f"wedge_{i}"] = np.full((8, 8), code(10**-density))
    binding = study_binding(fixture(4, 1, ASSUMPTIONS))
    calibration, _ = fit_reference(data, densities, binding, "synthetic")
    return calibration, binding, code


def test_study_reconstructs_held_out_data_in_linear_domain_before_combining():
    calibration, binding, code = synthetic_reference()
    # Two light levels in repeated stationary samples: merging density first is biased.
    transmittance = np.array([1.0, 0.01] * 4)
    codes = np.broadcast_to(code(transmittance)[:, None], (8, 40))
    expected = -np.log10(transmittance.mean())
    result = evaluate_codes(
        codes, calibration, binding, expected_density=expected, pitch_m=1e-3, bits=16
    )
    assert result["invalid_fraction"] == 0
    assert abs(result["bias_od_valid_only"]) < 0.002
    assert abs(expected - np.mean(-np.log10(transmittance))) > 0.5


def test_study_does_not_turn_rejected_or_nonpositive_samples_into_finite_density():
    calibration, binding, _ = synthetic_reference()
    outside = evaluate_codes(
        np.full((8, 40), 65535, np.uint16),
        calibration,
        binding,
        expected_density=1.5,
        pitch_m=1e-3,
        bits=16,
    )
    assert outside["invalid_fraction"] == 1
    assert outside["bias_od_valid_only"] is None
    assert outside["invalid_reason_counts"]["OUTSIDE_CALIBRATION"] == 40
    assert outside["invalid_reason_counts"]["ADC_SATURATED"] == 40


def test_research_binding_is_mesh_independent_but_never_relaxes_production_binding():
    coarse = fixture(4, 11, ASSUMPTIONS)
    fine = fixture(32, 21, ASSUMPTIONS)
    assert study_binding(coarse) == study_binding(fine)
    calibration, _, _ = synthetic_reference()
    with pytest.raises(ValueError, match="simulation_hash"):
        require_applicable(calibration, simulation_binding(coarse))
    changed = replace(fine, afe=replace(fine.afe, pole_hz=40_000))
    assert study_binding(changed) != study_binding(fine)


def test_observation_files_keep_actual_settling_gaps_and_reject_bad_crc(tmp_path):
    cfg = fixture(4, 10, ASSUMPTIONS)
    cfg.detector = replace(cfg.detector, shot_noise=False)
    path = tmp_path / "capture.zlraw"
    phases = [CapturePhase("one", 1e6, (2, 8)), CapturePhase("two", 1e5, (2, 8))]
    result = write_capture(path, cfg, phases, settle_samples=6)
    data, protocol = read_capture(path)
    assert result["phases"][0]["first_sample_index"] == "6"
    assert result["phases"][1]["first_sample_index"] == "28"
    assert data["one"].shape == (2, 8)
    assert protocol["numeric_grid_os"] == 4
    with path.open("r+b") as stream:
        stream.seek(-1, 2)
        stream.write(b"\xff")
    with pytest.raises(ValueError, match="corrupt"):
        read_capture(path)
