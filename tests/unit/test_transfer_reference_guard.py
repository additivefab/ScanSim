"""Explicit failed reference assessments cannot be silently consumed by reconstruction."""

import numpy as np
import pytest

from zlscan.contracts.calibration import Binding, CalibItem, CalibrationHeader
from zlscan.recon.density import Grid, reconstruct_density


def calibration(status):
    return CalibrationHeader(
        format_version=1,
        provenance="SIMULATED",
        calibration_id="assessed",
        created_at="fixture",
        binds_to=Binding(simulation_hash="fixture"),
        items={
            "transfer_lut": CalibItem(
                value={
                    "channels": {
                        "0": {
                            "code_knots": [0, 10],
                            "intensity_knots": [1, 11],
                            "reference_assessment": {"status": status},
                        }
                    }
                }
            ),
            "dark_raw_code": CalibItem(value={"0": {"median": [0]}}),
            "white_raw_code": CalibItem(value={"0": {"median": [10]}}),
        },
    )


@pytest.mark.parametrize("status", ["mismatch", "invented_pass"])
def test_failed_or_unknown_reference_assessment_is_rejected(status):
    with pytest.raises(ValueError, match="reference"):
        reconstruct_density(
            np.array([[5]]),
            np.array([[0.5]]),
            np.array([[0.5]]),
            Grid(1, 1, 1, 1),
            calibration(status),
            Binding(simulation_hash="fixture"),
            channel=0,
        )


@pytest.mark.parametrize("status", ["compatible", "not_assessed"])
def test_explicit_compatible_and_unassessed_retain_distinct_metadata(status):
    cal = calibration(status)
    result = reconstruct_density(
        np.array([[5]]),
        np.array([[0.5]]),
        np.array([[0.5]]),
        Grid(1, 1, 1, 1),
        cal,
        Binding(simulation_hash="fixture"),
        channel=0,
    )
    assert result.valid_fraction == 1
    assert (
        cal.items["transfer_lut"].value["channels"]["0"]["reference_assessment"]["status"] == status
    )
