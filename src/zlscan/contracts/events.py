"""Events with three independent evidence fields (docs/dev/03_contracts.md, section 4).

``origin`` (simulated or measured), ``visibility`` (observable on a real instrument or oracle
only) and ``derivation`` (sensor, inferred, command) are separate axes and must not be merged
into one enum: a simulated sensor event is observable yet not a real measurement, and a true
facet number is simulated and never observable.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, StrictInt, model_validator

from zlscan.contracts.time import SampleId
from zlscan.contracts.verdict import Verdict


class Origin(StrEnum):
    SIMULATED = "SIMULATED"
    MEASURED = "MEASURED"


class Visibility(StrEnum):
    OBSERVABLE = "OBSERVABLE"
    ORACLE_ONLY = "ORACLE_ONLY"


class Derivation(StrEnum):
    SENSOR = "SENSOR"
    INFERRED = "INFERRED"
    COMMAND = "COMMAND"


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    origin: Origin
    visibility: Visibility
    derivation: Derivation
    clock_domain: StrictInt
    count: StrictInt
    tick: StrictInt
    session_id: StrictInt
    epoch: StrictInt
    uncertainty_ticks: StrictInt | None = None
    capability_ref: str | None = None
    related_sample: SampleId | None = None
    payload: dict[str, Any] = {}

    @model_validator(mode="after")
    def _sensor_needs_a_declared_capability(self) -> Event:
        if (
            self.visibility is Visibility.OBSERVABLE
            and self.derivation is Derivation.SENSOR
            and not self.capability_ref
        ):
            raise ValueError("an observable sensor event needs capability_ref")
        return self

    def recon_verdict(self) -> Verdict:
        """Whether this event may be handed to the calibration and reconstruction."""
        from zlscan.contracts.event_gate import recon_event_gate

        return recon_event_gate(self.model_dump(mode="python"))
