"""Calibration file persistence and capture compatibility."""

import json
from pathlib import Path

from zlscan.contracts.calibration import CalibrationHeader, CalibrationSet, check_version
from zlscan.contracts.calibration import require_applicable as require_applicable


def load_calibration(path: Path) -> CalibrationHeader | CalibrationSet:
    """Read and validate a calibration file."""
    document = json.loads(path.read_bytes())
    if isinstance(document, dict) and document.get("format") == "zlscan.calibration-set/1":
        return CalibrationSet.model_validate(document)
    calibration = CalibrationHeader.model_validate(document)
    check_version(calibration)
    return calibration


def save_calibration(path: Path, calibration: CalibrationHeader | CalibrationSet) -> None:
    """Write a calibration file, refusing an unsupported format version."""
    if isinstance(calibration, CalibrationSet):
        CalibrationSet.model_validate(calibration.model_dump(mode="json"))
    else:
        check_version(calibration)
    path.write_text(calibration.model_dump_json(indent=2) + "\n", encoding="utf-8")
