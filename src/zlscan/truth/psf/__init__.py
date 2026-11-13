"""Spot (PSF) and collection efficiency (S3-T06).

The film transmittance seen by the spot is the Gaussian-weighted average of T around the beam
position, evaluated by fixed Gauss-Hermite quadrature (the weights sum to one, so a constant T
stays constant). The average is taken over transmittance, never over density. The quadrature is
an approximation of the convolution: more nodes converge to it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from zlscan.contracts.module import ModuleSpec

SPEC = ModuleSpec(
    name="truth.psf", interface={"x": "m", "y": "m", "T_eff": "1"}, state={},
    equation="T_eff = sum_ij w_i w_j T(x + sqrt2 sx a_i, y + sqrt2 sy a_j) (Gauss-Hermite)",
    parameters={"fwhm_x_m": "m", "fwhm_y_m": "m"},
    applicability="elliptical Gaussian core; no wide-tail glare (stage 5)",
    fidelity="L1", validation=["test_psf_constant_T", "test_psf_transmittance_not_density"],
)  # fmt: skip

FWHM_TO_SIGMA = 1 / 2.354820045


@dataclass(frozen=True)
class PsfConfig:
    fwhm_x_m: float
    fwhm_y_m: float
    nodes_x: int = 9
    nodes_y: int = 5


@lru_cache(maxsize=32)
def _quadrature(nodes_x: int, nodes_y: int, active_x: bool, active_y: bool):
    """Cache configuration-only nodes; returned arrays are immutable shared constants."""
    ax, wx = np.polynomial.hermite_e.hermegauss(nodes_x if active_x else 1)
    ay, wy = np.polynomial.hermite_e.hermegauss(nodes_y if active_y else 1)
    wx, wy = wx / wx.sum(), wy / wy.sum()
    for array in (ax, wx, ay, wy):
        array.setflags(write=False)
    return ax, wx, ay, wy


def effective_transmittance(
    lookup: Callable[[np.ndarray, np.ndarray], np.ndarray], cfg: PsfConfig,
    x: np.ndarray, y: np.ndarray,
) -> np.ndarray:  # fmt: skip
    sx, sy = cfg.fwhm_x_m * FWHM_TO_SIGMA, cfg.fwhm_y_m * FWHM_TO_SIGMA
    if sx == 0 and sy == 0:
        return lookup(x, y)
    ax, wx, ay, wy = _quadrature(cfg.nodes_x, cfg.nodes_y, bool(sx), bool(sy))
    out = np.zeros_like(x, dtype=float)
    for ai, wi in zip(ax, wx, strict=True):
        for aj, wj in zip(ay, wy, strict=True):
            out += wi * wj * lookup(x + sx * ai, y + sy * aj)
    return out
