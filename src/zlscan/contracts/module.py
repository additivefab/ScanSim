"""The interface every stateful module implements (docs/dev/02_architecture.md, section 4).

``advance`` is a pure function of (state, inputs, start tick, step count): it reads no global
state and no wall clock, so advancing in one go or in two chunks gives the same result (bitwise
for digital quantities, within a declared tolerance for analogue ones). That property is what
block-wise runs, checkpoints and replay rely on.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


class ModuleSpec(BaseModel):
    """Written before the model is implemented; the equations also go into 04_physics_spec.md."""

    model_config = ConfigDict(extra="forbid")

    name: str
    interface: dict[str, str]  # port name -> unit
    state: dict[str, str]  # state variable -> unit
    equation: str
    parameters: dict[str, str]  # parameter name -> unit
    applicability: str
    fidelity: Literal["L0", "L1", "L2"]
    validation: list[str]  # names of the tests that verify it


@runtime_checkable
class Module(Protocol):
    spec: ModuleSpec

    def initialize(self, cfg: Any, rng_factory: Any) -> Any: ...

    def advance(self, state: Any, inputs: Any, t0: int, n_steps: int) -> tuple[Any, Any]: ...

    def checkpoint(self, state: Any) -> bytes: ...

    def restore(self, blob: bytes) -> Any: ...

    def observables(self) -> list[Any]: ...
