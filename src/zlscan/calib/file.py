"""Calibration file persistence and capture compatibility."""

from pathlib import Path

from zlscan.contracts.calibration import CalibrationHeader, check_version
from zlscan.contracts.calibration import require_applicable as require_applicable


def load_calibration(path: Path) -> CalibrationHeader:
    """Read and validate a calibration file."""
    calibration = CalibrationHeader.model_validate_json(path.read_bytes())
    check_version(calibration)
    return calibration


def save_calibration(path: Path, calibration: CalibrationHeader) -> None:
    """Write a calibration file, refusing an unsupported format version."""
    check_version(calibration)
    path.write_text(calibration.model_dump_json(indent=2) + "\n", encoding="utf-8")
