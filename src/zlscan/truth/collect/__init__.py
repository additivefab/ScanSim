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
    # Relative efficiency table over the scan width (spec 13.4), ASSUMED; empty = off.
    # Replaces the parabolic roll-off when given; 1 is the nominal eta_collect.
    profile_u: tuple[float, ...] = ()
    profile_rel: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        if len(self.profile_u) != len(self.profile_rel):
            raise ValueError("collection table needs equally long position and value lists")
        if self.profile_u:
            u, r = np.asarray(self.profile_u, float), np.asarray(self.profile_rel, float)
            if len(u) < 2 or not (np.isfinite(u).all() and np.isfinite(r).all()):
                raise ValueError("collection table needs at least two finite points")
            if np.any(np.diff(u) <= 0) or u[0] < 0 or u[-1] > 1 or np.any(r < 0):
                raise ValueError(
                    "collection table positions must increase within 0..1; values >= 0"
                )
            if self.rolloff:
                raise ValueError("collection table replaces the roll-off; set only one")


def efficiency(cfg: CollectConfig, u: np.ndarray) -> np.ndarray:
    if cfg.profile_u:
        return cfg.eta_pre * cfg.eta_collect * np.interp(u, cfg.profile_u, cfg.profile_rel)
    return cfg.eta_pre * cfg.eta_collect * (1 - cfg.rolloff * (2 * u - 1) ** 2)
