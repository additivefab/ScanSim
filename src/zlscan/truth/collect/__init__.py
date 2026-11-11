"""Collection efficiency (S3-T06): a position-dependent factor plus a constant stray power.

``eta(x) = eta0 * (1 - rolloff * (2 u - 1)^2)`` with ``u`` the position across the scan width.
Stray light is a constant power independent of the signal.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from zlscan.contracts.module import ModuleSpec

SPEC = ModuleSpec(
    name="truth.collect", interface={"x": "m", "eta": "1"}, state={},
    equation="eta = eta_pre * eta_collect * (1 - rolloff (2u-1)^2); P_stray constant",
    parameters={"eta_pre": "1", "eta_collect": "1", "rolloff": "1", "stray_w": "W"},
    applicability="parabolic roll-off stand-in; no measured table",
    fidelity="L1", validation=["test_collect_rolloff", "test_collect_stray_constant"],
)  # fmt: skip


@dataclass(frozen=True)
class CollectConfig:
    eta_pre: float
    eta_collect: float
    rolloff: float = 0.0
    stray_w: float = 0.0


def efficiency(cfg: CollectConfig, u: np.ndarray) -> np.ndarray:
    return cfg.eta_pre * cfg.eta_collect * (1 - cfg.rolloff * (2 * u - 1) ** 2)
