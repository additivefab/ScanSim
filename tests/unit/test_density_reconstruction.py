import numpy as np
import pytest

from zlscan.contracts.calibration import Binding, CalibItem, CalibrationHeader
from zlscan.contracts.flags import InvalidFlag
from zlscan.recon.density import Grid, reconstruct_density


def calibration():
    return CalibrationHeader(
        format_version=1,
        provenance="SIMULATED",
        calibration_id="cal",
        created_at="2026-11-12T00:00:00Z",
        binds_to=Binding(simulation_hash="bench"),
        items={
            "transfer_lut": CalibItem(
                value={
                    "channels": {
                        "0": {
                            "code_knots": [0, 10, 20, 30],
                            "intensity_knots": [0.01, 0.1, 1.0, 10.0],
                            "white_outlier_columns": [],
                        }
                    },
                    "extrapolation": "invalid",
                }
            ),
            "dark_raw_code": CalibItem(value={"0": {"median": [0, 0]}}),
            "white_raw_code": CalibItem(value={"0": {"median": [20, 20]}}),
        },
    )


def test_linearized_samples_are_merged_before_density():
    cal = calibration()
    result = reconstruct_density(
        np.array([[10, 20]]),
        np.array([[0.5, 0.5]]),
        np.array([[0.5, 0.5]]),
        Grid(1, 1, 1, 1),
        cal,
        cal.binds_to,
        channel=0,
    )
    expected = ((0.1 - 0.01) / (1.0 - 0.01) + 1) / 2
    assert result.transmittance[0, 0] == pytest.approx(expected)
    assert result.density[0, 0] == pytest.approx(-np.log10(expected))
    assert not result.flags.any()


def test_invalid_values_keep_independent_reasons_and_no_finite_density():
    cal = calibration()
    result = reconstruct_density(
        np.array([[0, 31]]),
        np.array([[0.5, 1.5]]),
        np.array([[0.5, 0.5]]),
        Grid(3, 1, 1, 1),
        cal,
        cal.binds_to,
        channel=0,
        sample_flags=np.array([[0, InvalidFlag.ADC_SATURATED]], dtype=np.uint16),
    )
    assert np.isnan(result.density).all()
    assert result.flags[0, 0] & InvalidFlag.NONPOSITIVE_TRANSMITTANCE
    assert result.flags[0, 1] & InvalidFlag.OUTSIDE_CALIBRATION
    assert result.flags[0, 1] & InvalidFlag.ADC_SATURATED
    assert result.flags[0, 2] & InvalidFlag.RESAMPLING_SUPPORT


def test_incompatible_binding_is_refused_before_reconstruction():
    cal = calibration()
    with pytest.raises(ValueError, match="simulation_hash"):
        reconstruct_density(
            np.array([[10, 20]]),
            np.array([[0.5, 0.5]]),
            np.array([[0.5, 0.5]]),
            Grid(1, 1, 1, 1),
            cal,
            Binding(simulation_hash="different"),
            channel=0,
        )
