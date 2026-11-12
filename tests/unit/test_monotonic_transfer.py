"""Observed lookup support cannot manufacture information in ambiguous reference intervals."""

import numpy as np
import pytest

from zlscan.calib.monotonic import UnidentifiableTransfer, fit_monotonic_transfer


def fit(codes, signal, uncertainty=0.5, bounds=None):
    return fit_monotonic_transfer(
        np.asarray(codes),
        np.asarray(signal),
        code_half_width=np.broadcast_to(uncertainty, len(codes)),
        signal_bounds=np.column_stack([signal, signal]) if bounds is None else bounds,
        lut_points=257,
    )


@pytest.mark.parametrize("sign", [1, -1])
def test_observed_log_table_matches_held_out_points_for_both_polarities(sign):
    signal = [0.0, 1.0, 1.0, 0.1, 0.01, 0.001, 0.0001]
    codes = 30000 + sign * 4000 * np.log10(np.maximum(signal, 1e-5))
    model = fit(codes, signal)
    targets = np.array([0.5, 1.5, 2.5, 3.5])
    held_codes = 30000 - sign * 4000 * targets
    linear = np.interp(held_codes, model.code_knots, model.intensity_knots) - 1
    assert np.max(np.abs(-np.log10(linear) - targets)) < 0.001
    assert model.normalization["kind"] == "relative_transmittance"
    assert model.normalization["dark_signal"] == 1
    assert model.normalization["white_signal"] == 2
    assert np.all(np.diff(model.code_knots) > 0)


def test_duplicate_reference_uncertainty_is_retained_and_inconsistency_is_rejected():
    model = fit([0, 100, 101, 60, 30], [0, 1, 1, 0.1, 0.01], uncertainty=1.0)
    assert model.reference_groups[-1]["source_indices"] == (1, 2)
    assert model.reference_groups[-1]["code_bounds"] == (99, 102)
    with pytest.raises(UnidentifiableTransfer, match="duplicate"):
        fit([0, 100, 110, 60, 30], [0, 1, 1, 0.1, 0.01])


def test_ambiguous_dark_region_is_excluded_while_brighter_support_remains():
    model = fit([0, 100, 70, 30, 1], [0, 1, 0.1, 0.01, 0.001], uncertainty=2.0)
    assert model.excluded_code_ranges
    assert model.unidentifiable_edges
    assert model.valid_code_ranges
    assert any(lo <= 1 <= hi for lo, hi in model.excluded_code_ranges)
    with pytest.raises(UnidentifiableTransfer, match="support"):
        fit([0, 1, 0.8, 0.6], [0, 1, 0.1, 0.01], uncertainty=2.0)


def test_interior_uncertainty_overlap_does_not_create_a_bridge():
    model = fit([0, 100, 80, 40, 39, 10], [0, 1, 0.1, 0.01, 0.001, 0.0001], uncertainty=1.0)
    assert any(lo <= 39.5 <= hi for lo, hi in model.excluded_code_ranges)
    assert len(model.valid_code_ranges) >= 2


@pytest.mark.parametrize(
    "codes,signal,uncertainty",
    [
        ([0, 100, 20, 60], [0, 1, 0.1, 0.01], 0.5),
        ([0, 100, 60], [0, 1, 0.1], -1),
        ([0, float("nan"), 60], [0, 1, 0.1], 0.5),
    ],
)
def test_invalid_observations_are_not_monotonized_silently(codes, signal, uncertainty):
    with pytest.raises(ValueError):
        fit(codes, signal, uncertainty)


def test_reconstruction_keeps_internal_gaps_invalid_and_uses_relative_normalization():
    from dataclasses import asdict

    from zlscan.contracts.calibration import Binding, CalibItem, CalibrationHeader
    from zlscan.contracts.flags import InvalidFlag
    from zlscan.recon.density import Grid, reconstruct_density

    model = fit([0, 100, 80, 40, 39, 10], [0, 1, 0.1, 0.01, 0.001, 0.0001], uncertainty=1.0)
    calibration = CalibrationHeader(
        format_version=2,
        provenance="SIMULATED",
        calibration_id="table",
        created_at="fixture",
        binds_to=Binding(simulation_hash="fixture"),
        items={"transfer_lut": CalibItem(value={"channels": {"0": asdict(model)}})},
    )
    codes = np.array([[5, 20, 39, 40, 60, 101]])
    image = reconstruct_density(
        codes,
        (np.arange(6)[None, :] + 0.5),
        np.full(codes.shape, 0.5),
        Grid(6, 1, 1, 1),
        calibration,
        calibration.binds_to,
        channel=0,
    )
    for i in [0, 2, 3, 5]:
        assert image.flags[0, i] & int(InvalidFlag.OUTSIDE_CALIBRATION)
        assert np.isnan(image.density[0, i])
    for i in [1, 4]:
        assert image.flags[0, i] == 0
        assert np.isfinite(image.density[0, i])


def test_uncertainty_must_be_declared_and_version_one_cannot_ignore_new_support():
    from dataclasses import asdict

    from zlscan.contracts.calibration import Binding, CalibItem, CalibrationHeader, check_version

    with pytest.raises(UnidentifiableTransfer, match="not been declared"):
        fit_monotonic_transfer(
            [0, 10, 5],
            [0, 1, 0.1],
            code_half_width=None,
            signal_bounds=[[0, 0], [1, 1], [0.1, 0.1]],
            lut_points=257,
        )
    model = fit([0, 100, 50, 20], [0, 1, 0.1, 0.01])
    calibration = CalibrationHeader(
        format_version=2,
        provenance="SIMULATED",
        calibration_id="v2",
        created_at="fixture",
        binds_to=Binding(simulation_hash="fixture"),
        items={"transfer_lut": CalibItem(value={"channels": {"0": asdict(model)}})},
    )
    check_version(calibration)
    with pytest.raises(ValueError, match="version 2"):
        check_version(calibration.model_copy(update={"format_version": 1}))
    with pytest.raises(ValueError, match="requires"):
        check_version(calibration.model_copy(update={"items": {}}))


def test_overlapping_signal_uncertainty_also_excludes_interpolation_support():
    signal = np.array([0.0, 1.0, 0.1, 0.01, 0.001])
    bounds = np.column_stack([signal, signal])
    bounds[2] = [0.03, 0.2]
    bounds[3] = [0.005, 0.05]
    model = fit([0, 100, 70, 40, 10], signal, bounds=bounds)
    assert any("signal uncertainty" in edge["reason"] for edge in model.unidentifiable_edges)
    assert any(low <= 55 <= high for low, high in model.excluded_code_ranges)
    assert model.valid_code_ranges
