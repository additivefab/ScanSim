import pytest

from zlscan.calib.file import load_calibration, require_applicable, save_calibration
from zlscan.contracts.calibration import Binding, CalibrationHeader, Provenance


@pytest.fixture
def binding():
    return Binding(
        hardware_hash="hardware",
        wiring_rev="revision",
        firmware_hash="firmware",
        register_dump_hash="registers",
        channel_roles={"0": "signal"},
        gain_settings={"pmt": "g1"},
        epoch_range=(2, 4),
    )


def test_calibration_file_round_trip(tmp_path, binding):
    path = tmp_path / "calibration.json"
    calibration = CalibrationHeader(
        format_version=1,
        provenance=Provenance.MEASURED,
        calibration_id="cal-1",
        created_at="2026-11-12T00:00:00Z",
        binds_to=binding,
    )
    save_calibration(path, calibration)
    assert load_calibration(path) == calibration


@pytest.mark.parametrize(
    "field,value",
    [
        ("hardware_hash", "other"),
        ("gain_settings", {"pmt": "g2"}),
        ("epoch_range", (1, 5)),
        ("channel_roles", {"0": "reference"}),
    ],
)
def test_rejects_incompatible_capture(binding, field, value):
    calibration = CalibrationHeader(
        format_version=1,
        provenance=Provenance.MEASURED,
        calibration_id="cal-1",
        created_at="2026-11-12T00:00:00Z",
        binds_to=binding,
    )
    require_applicable(calibration, binding.model_copy(update={"epoch_range": (2, 3)}))
    with pytest.raises(ValueError, match=field):
        require_applicable(calibration, binding.model_copy(update={field: value}))


def test_simulated_identity_is_distinct_from_hardware_and_model_is_refused():
    capture = Binding(simulation_hash="simulation", epoch_range=(0, 0))
    calibration = CalibrationHeader(
        format_version=1,
        provenance=Provenance.SIMULATED,
        calibration_id="cal-1",
        created_at="2026-11-12T00:00:00Z",
        binds_to=capture,
    )
    require_applicable(calibration, capture)
    with pytest.raises(ValueError, match="simulation_hash"):
        require_applicable(calibration, Binding())
    model = calibration.model_copy(update={"provenance": Provenance.MODEL})
    with pytest.raises(ValueError, match="MODEL"):
        require_applicable(model, capture)
    require_applicable(model, capture, allow_model_calibration=True)
