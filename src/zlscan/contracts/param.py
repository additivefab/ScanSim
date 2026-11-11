"""A parameter value together with the evidence for it (docs/dev/03_contracts.md, section 1).

Rules enforced here, because they are the ones that were broken before:

* unknown is ``None`` (never 0), and an unknown value has no evidence: ``value is None`` if and
  only if ``evidence is None``;
* evidence that points at a document (project text, photo, schematic, datasheet) must name it;
* a measured value must say when and how it was measured.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, model_validator

T = TypeVar("T")


class Evidence(StrEnum):
    PROJECT_TEXT = "project_text"
    PHOTO = "photo_observation"
    SCHEMATIC = "schematic_nominal"
    DATASHEET = "datasheet"
    DERIVED = "derived"
    ASSUMED = "assumed"
    MEASURED = "measured"


_NEEDS_SOURCE = frozenset(
    {Evidence.PROJECT_TEXT, Evidence.PHOTO, Evidence.SCHEMATIC, Evidence.DATASHEET}
)


class Param(BaseModel, Generic[T]):
    model_config = ConfigDict(extra="forbid")

    value: T | None = None
    unit: str = ""
    evidence: Evidence | None = None
    source_id: str | None = None
    locator: str | None = None
    uncertainty: float | None = None
    valid_for: str | None = None
    measured_at: str | None = None
    method: str | None = None
    status: Literal["confirmed", "pending", "withdrawn"] = "pending"

    @model_validator(mode="after")
    def _check_evidence(self) -> Param[T]:
        if (self.value is None) != (self.evidence is None):
            raise ValueError("value and evidence must both be set or both be None (unknown)")
        if self.evidence in _NEEDS_SOURCE and not self.source_id:
            raise ValueError(f"evidence {self.evidence.value!r} requires source_id")
        if self.evidence is Evidence.MEASURED and not (self.measured_at and self.method):
            raise ValueError("a measured value requires measured_at and method")
        return self
