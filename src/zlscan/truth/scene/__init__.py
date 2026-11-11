"""Film scene (S3-T03): a density map with physical size, evaluated as transmittance.

``T = 10 ** -D``. Lookups interpolate the transmittance, never the density. Positions outside the
film return ``off_film_T`` (default 1.0, a clear pass-through; a film holder that blocks light is
``off_film_T = 0.0``). A lookup is a pure function of position, so it needs no stored state.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.ndimage import map_coordinates

from zlscan.contracts.module import ModuleSpec

SPEC = ModuleSpec(
    name="truth.scene", interface={"x": "m", "y": "m", "T": "1"}, state={},
    equation="T(x,y) = 10^-D(x,y), bilinear in T; outside the film T = off_film_T",
    parameters={"size": "m", "off_film_T": "1"},
    applicability="quantitative density/transmittance maps; 8-bit pictures are not quantitative",
    fidelity="L1", validation=["test_scene_constant_T", "test_scene_off_film_rule"],
)  # fmt: skip


@dataclass(frozen=True)
class Scene:
    transmittance_map: np.ndarray  # (ny, nx), values in [0, 1]
    width_m: float
    height_m: float
    off_film_T: float = 1.0

    @classmethod
    def from_density(cls, density: np.ndarray, width_m: float, height_m: float,
                     off_film_T: float = 1.0) -> Scene:  # fmt: skip
        d = np.asarray(density, dtype=float)
        if d.ndim != 2 or width_m <= 0 or height_m <= 0:
            raise ValueError("density must be 2-D with positive physical size")
        if np.any(d < 0):
            raise ValueError("negative density")
        return cls(10.0 ** (-d), width_m, height_m, off_film_T)

    def lookup(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        ny, nx = self.transmittance_map.shape
        fx = np.asarray(x, dtype=float) / self.width_m * nx - 0.5
        fy = np.asarray(y, dtype=float) / self.height_m * ny - 0.5
        inside = (np.asarray(x) >= 0) & (np.asarray(x) <= self.width_m)
        inside &= (np.asarray(y) >= 0) & (np.asarray(y) <= self.height_m)
        val = map_coordinates(self.transmittance_map, [fy, fx], order=1, mode="nearest")
        return np.where(inside, val, self.off_film_T)


def uniform(density: float, width_m: float, height_m: float, n: int = 8) -> Scene:
    return Scene.from_density(np.full((n, n), density), width_m, height_m)


def step_wedge(densities: list[float], width_m: float, height_m: float, n: int = 64) -> Scene:
    """Vertical bands of constant density from left to right."""
    cols = np.repeat(np.array(densities, dtype=float), n)
    return Scene.from_density(np.tile(cols, (n, 1)), width_m, height_m)


def edge(low: float, high: float, width_m: float, height_m: float, n: int = 256) -> Scene:
    d = np.where(np.arange(n) < n // 2, low, high).astype(float)
    return Scene.from_density(np.tile(d, (n, 1)), width_m, height_m)
