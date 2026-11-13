"""Film scene (S3-T03): a density map with physical size, evaluated as transmittance.

``T = 10 ** -D``. Lookups interpolate the transmittance, never the density. Positions outside the
film return ``off_film_T`` (default 1.0, a clear pass-through; a film holder that blocks light is
``off_film_T = 0.0``). A lookup is a pure function of position, so it needs no stored state.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

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
    legacy_nearest_boundary: bool = False  # explicit package-A nearest-edge extension

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
        """Interpolate transmittance at physical coordinates.

        This is the order-1, ``mode="nearest"`` operation previously delegated to
        ``scipy.ndimage.map_coordinates``.  Keeping the four-point interpolation here
        avoids rebuilding SciPy's geometric-transform iterator for every PSF quadrature
        node.  The explicit clipping preserves nearest-edge behavior before the
        physical outside-film rule is applied.
        """
        ny, nx = self.transmittance_map.shape
        x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=float)
        fx = x / self.width_m * nx - 0.5
        fy = y / self.height_m * ny - 0.5
        inside = (x >= 0) & (x <= self.width_m)
        inside &= (y >= 0) & (y <= self.height_m)
        fx = np.clip(fx, 0, nx - 1)
        fy = np.clip(fy, 0, ny - 1)
        x0 = np.floor(fx).astype(np.intp)
        y0 = np.floor(fy).astype(np.intp)
        x1 = np.minimum(x0 + 1, nx - 1)
        y1 = np.minimum(y0 + 1, ny - 1)
        wx = fx - x0
        wy = fy - y0
        val = (
            self.transmittance_map[y0, x0] * (1 - wx) * (1 - wy)
            + self.transmittance_map[y0, x1] * wx * (1 - wy)
            + self.transmittance_map[y1, x0] * (1 - wx) * wy
            + self.transmittance_map[y1, x1] * wx * wy
        )
        return val if self.legacy_nearest_boundary else np.where(inside, val, self.off_film_T)


def uniform(density: float, width_m: float, height_m: float, n: int = 8) -> Scene:
    return Scene.from_density(np.full((n, n), density), width_m, height_m)


def step_wedge(densities: list[float], width_m: float, height_m: float, n: int = 64) -> Scene:
    """Vertical bands of constant density from left to right."""
    cols = np.repeat(np.array(densities, dtype=float), n)
    return Scene.from_density(np.tile(cols, (n, 1)), width_m, height_m)


def edge(low: float, high: float, width_m: float, height_m: float, n: int = 256) -> Scene:
    d = np.where(np.arange(n) < n // 2, low, high).astype(float)
    return Scene.from_density(np.tile(d, (n, 1)), width_m, height_m)
