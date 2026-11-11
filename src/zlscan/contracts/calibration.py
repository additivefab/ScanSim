"""Header and items of a calibration file (docs/dev/03_contracts.md, section 6).

The file says where it came from (``MODEL`` is derived from the truth and exists only for
regression), which hardware, firmware, register state, gain settings and epochs it is valid for,
and per item its value and uncertainty. An item that cannot be estimated from the given data is
``unidentifiable`` and carries no number.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator


class Provenance(StrEnum):
    MODEL = "MODEL"
    SIMULATED = "SIMULATED"
    MEASURED = "MEASURED"


class Binding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hardware_hash: str | None = None
    wiring_rev: str | None = None
    firmware_hash: str | None = None
    register_dump_hash: str | None = None
    channel_roles: dict[str, str | None] = {}
    gain_settings: dict[str, str] = {}
    epoch_range: tuple[int, int] = (0, 0)


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
