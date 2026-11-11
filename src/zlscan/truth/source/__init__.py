"""Laser source (S3-T04): power with ripple and a per-line fluctuation; always on or gated.

``P(t) = P0 * (1 + ripple * sin(2 pi f_r t)) * (1 + sigma_line * z[line])``. ``z`` is a unit normal
per line from the counter RNG, so it does not depend on chunking. The reference-light channel is
off by default (nothing is invented for hardware that is not known to exist).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from zlscan.contracts.module import ModuleSpec
from zlscan.engine.rng import CounterRng

SPEC = ModuleSpec(
    name="truth.source", interface={"t": "s", "P": "W"}, state={},
    equation="P = P0 (1 + a sin(2 pi f t)) (1 + s z_line), zero while gated off",
    parameters={"power_w": "W", "ripple": "1", "ripple_hz": "Hz", "line_sigma": "1"},
    applicability="amplitude fluctuations only; no mode hopping, no drift",
    fidelity="L1", validation=["test_source_ripple", "test_source_line_fluct_chunk_independent"],
)  # fmt: skip


@dataclass(frozen=True)
class SourceConfig:
    power_w: float
    ripple: float = 0.0
    ripple_hz: float = 0.0
    line_sigma: float = 0.0
    gated: bool = True
    reference_channel: bool = False


def power(cfg: SourceConfig, t_s: np.ndarray, line: np.ndarray, rng: CounterRng, domain: int,
          on_window: np.ndarray, laser_on: np.ndarray) -> np.ndarray:  # fmt: skip
    """Optical power at times ``t_s``; ``line`` holds each step's line number."""
    p = np.full_like(t_s, cfg.power_w)
    if cfg.ripple:
        p = p * (1 + cfg.ripple * np.sin(2 * math.pi * cfg.ripple_hz * t_s))
    if cfg.line_sigma:
        uniq = np.unique(line)
        z = {int(k): float(rng.normal("source_line", domain, int(k), 1)[0]) for k in uniq}
        p = p * (1 + cfg.line_sigma * np.array([z[int(k)] for k in line]))
    on = laser_on & (on_window if cfg.gated else True)
    return np.where(on, np.maximum(p, 0.0), 0.0)
