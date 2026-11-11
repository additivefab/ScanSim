"""Analogue front end (S3-T08): log or linear stage, driver map, anti-alias poles.

    log:    V = V0 + s S log10(max(I, I_floor) / I_ref)      (s = +/-1)
    linear: V = V0 + R_T I
    then:   fixed first-order pole (or the current-dependent form of package A), driver
            ``V = g V + b`` with a clip flag, then anti-alias first-order poles.

Every first-order stage uses the exact recursion for piecewise-linear input; its state lives in
the ``FirstOrder`` delay line and survives chunking and checkpoints. Filter state is never reset
at a line start.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from zlscan.contracts.module import ModuleSpec
from zlscan.engine.filters import FirstOrder

SPEC = ModuleSpec(
    name="truth.afe", interface={"I": "A", "V": "V"}, state={"poles": "V"},
    equation="V = V0 + s S log10(I/Iref); first-order poles by exact recursion; V' = g V + b",
    parameters={"kind": "log|linear", "polarity": "+1|-1", "slope": "V/dec", "ref_a": "A"},
    applicability="no log-law error, no temperature drift, no AC coupling (stage 5)",
    fidelity="L1", validation=["test_afe_dc", "test_afe_step_convergence", "test_afe_polarity"],
)  # fmt: skip


@dataclass(frozen=True)
class AfeConfig:
    kind: str  # "log" | "linear"
    polarity: int = 1
    v0: float = 0.0
    slope_v_per_dec: float = 0.25
    i_ref_a: float = 1e-6
    i_floor_a: float = 1e-12
    r_t_ohm: float = 1e4
    pole_hz: float | None = None
    current_dependent: bool = False
    knee_a: float = 1e-8
    floor_hz: float = 2e4
    driver_gain: float = 1.0
    driver_offset_v: float = 0.0
    clip_min_v: float | None = None
    clip_max_v: float | None = None
    aa_poles_hz: tuple[float, ...] = ()


class Afe:
    def __init__(self, cfg: AfeConfig, dt_s: float) -> None:
        if cfg.kind not in ("log", "linear"):
            raise ValueError("kind must be 'log' or 'linear'")
        if cfg.polarity not in (-1, 1):
            raise ValueError("polarity must be +1 or -1")
        self.cfg, self.dt = cfg, dt_s
        self.pole = (
            FirstOrder(cfg.pole_hz, dt_s) if cfg.pole_hz and not cfg.current_dependent else None
        )
        self.vstate: tuple[float, float] | None = (
            None  # (value, previous input) for the variable pole
        )
        self.aa = [FirstOrder(f, dt_s) for f in cfg.aa_poles_hz]

    def static(self, current: np.ndarray) -> np.ndarray:
        c = self.cfg
        if c.kind == "log":
            return c.v0 + c.polarity * c.slope_v_per_dec * np.log10(
                np.maximum(current, c.i_floor_a) / c.i_ref_a
            )
        return c.v0 + c.r_t_ohm * current

    def _variable_pole(self, x: np.ndarray, current: np.ndarray) -> np.ndarray:
        c = self.cfg
        bw = np.maximum(c.floor_hz, c.pole_hz * np.minimum(1.0, current / c.knee_a))
        u = 2 * np.pi * bw * self.dt
        a = np.exp(-u)
        ratio = -np.expm1(-u) / u
        out = np.empty_like(x)
        value, prev = self.vstate if self.vstate is not None else (x[0], x[0])
        for k in range(len(x)):
            value = a[k] * value + (ratio[k] - a[k]) * prev + (1 - ratio[k]) * x[k]
            prev = x[k]
            out[k] = value
        self.vstate = (value, prev)
        return out

    def process(self, current: np.ndarray):
        v = self.static(current)
        if self.pole is not None:
            v = self.pole.process(v)
        elif self.cfg.current_dependent and self.cfg.pole_hz:
            v = self._variable_pole(v, current)
        v = self.cfg.driver_gain * v + self.cfg.driver_offset_v
        clipped = np.zeros(len(v), dtype=bool)
        if self.cfg.clip_min_v is not None:
            clipped |= v < self.cfg.clip_min_v
            v = np.maximum(v, self.cfg.clip_min_v)
        if self.cfg.clip_max_v is not None:
            clipped |= v > self.cfg.clip_max_v
            v = np.minimum(v, self.cfg.clip_max_v)
        for f in self.aa:
            v = f.process(v)
        return v, clipped

    def state(self) -> dict:
        return {
            "pole": None if self.pole is None else self.pole.state(),
            "vstate": None if self.vstate is None else list(self.vstate),
            "aa": [f.state() for f in self.aa],
        }

    def set_state(self, s: dict) -> None:
        if self.pole is not None:
            self.pole.set_state(s["pole"])
        self.vstate = None if s["vstate"] is None else (s["vstate"][0], s["vstate"][1])
        for f, z in zip(self.aa, s["aa"], strict=True):
            f.set_state(z)


def two_pole_corner(f1: float, f2: float) -> float:
    """Helper for tests: the -3 dB frequency of two identical poles."""
    return f1 * math.sqrt(math.sqrt(2) - 1)
