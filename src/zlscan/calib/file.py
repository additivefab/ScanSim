"""Calibration file persistence and capture compatibility."""

from pathlib import Path

from zlscan.contracts.calibration import Binding, CalibrationHeader, Provenance

_FORMAT_VERSION = 1
_HARDWARE_FIELDS = ("hardware_hash", "wiring_rev", "firmware_hash", "register_dump_hash")


def _check_version(calibration: CalibrationHeader) -> None:
    if calibration.format_version != _FORMAT_VERSION:
        raise ValueError(f"unsupported calibration format version {calibration.format_version}")


def load_calibration(path: Path) -> CalibrationHeader:
    """Read and validate a calibration file."""
    calibration = CalibrationHeader.model_validate_json(path.read_bytes())
    _check_version(calibration)
    return calibration


def save_calibration(path: Path, calibration: CalibrationHeader) -> None:
    """Write a calibration file, refusing an unsupported format version."""
    _check_version(calibration)
    path.write_text(calibration.model_dump_json(indent=2) + "\n", encoding="utf-8")


def require_applicable(
    calibration: CalibrationHeader,
    capture: Binding,
    *,
    allow_model_calibration: bool = False,
) -> None:
    """Reject a calibration that cannot apply to every epoch in the capture."""
    _check_version(calibration)
    if not calibration.accepted_for_reconstruction(allow_model_calibration):
        raise ValueError("MODEL calibration is disabled")
    bound = calibration.binds_to
    if bound.simulation_hash is not None or capture.simulation_hash is not None:
        if (
            bound.simulation_hash is None
            or bound.simulation_hash != capture.simulation_hash
            or calibration.provenance is Provenance.MEASURED
        ):
            raise ValueError("incompatible calibration simulation_hash")
    else:
        if calibration.provenance is Provenance.SIMULATED:
            raise ValueError("SIMULATED calibration requires simulation_hash")
        for field in _HARDWARE_FIELDS:
            value = getattr(bound, field)
            if value is None or value != getattr(capture, field):
                raise ValueError(f"incompatible calibration {field}")
    for field in ("gain_settings", "channel_roles"):
        if getattr(bound, field) != getattr(capture, field):
            raise ValueError(f"incompatible calibration {field}")
    first, last = capture.epoch_range
    valid_first, valid_last = bound.epoch_range
    if not valid_first <= first <= last <= valid_last:
        raise ValueError("incompatible calibration epoch_range")
