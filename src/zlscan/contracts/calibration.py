"""Header and items of a calibration file (docs/dev/03_contracts.md, section 6).

The file says where it came from (``MODEL`` is derived from the truth and exists only for
regression), which hardware, firmware, register state, gain settings and epochs it is valid for,
and per item its value and uncertainty. An item that cannot be estimated from the given data is
``unidentifiable`` and carries no number.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Provenance(StrEnum):
    MODEL = "MODEL"
    SIMULATED = "SIMULATED"
    MEASURED = "MEASURED"


class Binding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    simulation_hash: str | None = None
    hardware_hash: str | None = None
    wiring_rev: str | None = None
    firmware_hash: str | None = None
    register_dump_hash: str | None = None
    channel_roles: dict[str, str | None] = {}
    gain_settings: dict[str, str] = {}
    epoch_range: tuple[int, int] = (0, 0)

    @model_validator(mode="after")
    def _ordered_epochs(self) -> Binding:
        first, last = self.epoch_range
        if not 0 <= first <= last:
            raise ValueError("epoch_range must be ordered and non-negative")
        return self


class CaptureDescription(BaseModel):
    """Operator-declared capture extent and identity, bound to a specific raw file."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    raw_sha256: str
    binding: Binding
    lines: int = Field(gt=0)


class CalibItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "unidentifiable"] = "ok"
    value: Any | None = None
    uncertainty: float | None = None
    valid_range: tuple[float, float] | None = None
    method: str | None = None

    @model_validator(mode="after")
    def _unidentifiable_has_no_value(self) -> CalibItem:
        if self.status == "unidentifiable" and self.value is not None:
            raise ValueError("an unidentifiable item must not carry a value")
        return self


class CalibrationHeader(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format_version: int
    provenance: Provenance
    calibration_id: str
    created_at: str
    binds_to: Binding
    valid_conditions: dict[str, Any] = {}
    generator: dict[str, Any] = {}
    items: dict[str, CalibItem] = {}

    def accepted_for_reconstruction(self, allow_model: bool = False) -> bool:
        """A MODEL file is refused unless the caller explicitly allows it (legacy tests only)."""
        return self.provenance is not Provenance.MODEL or allow_model

    def matches_session(
        self,
        *,
        hardware_hash: str | None,
        wiring_rev: str | None,
        firmware_hash: str | None,
        register_dump_hash: str | None,
        gain_settings: dict[str, str],
        epoch: int,
    ) -> bool:
        """True only when every bound field is known and equal to the session's."""
        b = self.binds_to
        pairs = (
            (b.hardware_hash, hardware_hash),
            (b.wiring_rev, wiring_rev),
            (b.firmware_hash, firmware_hash),
            (b.register_dump_hash, register_dump_hash),
        )
        if any(bound is None or bound != actual for bound, actual in pairs):
            return False
        if b.gain_settings != gain_settings:
            return False
        return b.epoch_range[0] <= epoch <= b.epoch_range[1]


_FORMAT_VERSION = 1
_HARDWARE_FIELDS = ("hardware_hash", "wiring_rev", "firmware_hash", "register_dump_hash")


def check_version(calibration: CalibrationHeader) -> None:
    if calibration.format_version != _FORMAT_VERSION:
        raise ValueError(f"unsupported calibration format version {calibration.format_version}")


def require_applicable(
    calibration: CalibrationHeader,
    capture: Binding,
    *,
    allow_model_calibration: bool = False,
) -> None:
    """Reject a calibration that cannot apply to every epoch in the capture."""
    check_version(calibration)
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
