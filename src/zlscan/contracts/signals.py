"""Signal registry entry (docs/dev/03_contracts.md, section 9).

Defaults are conservative: a signal is not interpolated for display and is recomputed on demand
rather than stored, unless its declaration says otherwise.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from zlscan.contracts.events import Derivation, Origin, Visibility


class SignalDecl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signal_id: str
    component_id: str
    port_id: str
    physical_domain: Literal["optical", "analog", "digital", "mechanical", "control"]
    quantity: str
    unit: str
    value_type: Literal["scalar", "vector", "event", "logic", "sample_block"]
    origin: Origin
    visibility: Visibility
    derivation: Derivation
    parameter_evidence: str
    model_id: str
    model_version: str
    clock_domain: int
    native_step_policy: str
    time_support: str | None = None
    alignment_uncertainty: float | None = None
    display_interpolation: Literal["none", "hold", "linear"] = "none"
    recording_policy: Literal["full", "summary", "on_demand", "none"] = "on_demand"
