"""PMT (S3-T07): Poisson primary electrons, Gamma multiplication, dark events, limit warning.

    mu = (P QE / E_photon + R_dark) dt          n = Poisson(mu)
    q | n = Gamma(shape n / (F - 1), scale G (F - 1))   (F = 1: q = n G)
    I = q e / dt

Both draws are inverse transforms of counter-RNG uniforms keyed by the absolute step, so the
result does not depend on chunking and no generator state is checkpointed. Poisson is exact (no
Gaussian shortcut at low counts). The anode limit only raises a warning flag; it is not a
space-charge model. The optional bandwidth pole is a first-order filter and is ``ASSUMED``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.signal import lfilter
from scipy.stats import gamma as gamma_dist
from scipy.stats import poisson

from zlscan.contracts.module import ModuleSpec
from zlscan.engine.rng import CounterRng

H = 6.62607015e-34
C = 299792458.0
E = 1.602176634e-19

SPEC = ModuleSpec(
    name="truth.detector", interface={"P": "W", "I": "A"}, state={"pole": "A"},
    equation="n~Poisson(mu), q~Gamma, I_bin = q e / dt; "
             "optional dy/dt=(I_bin-y)/tau BEFORE log; E[q] = mu G, Var[q] = F mu G^2",
    parameters={"qe": "1", "gain": "1", "excess_noise_factor": "1", "dark_current_a": "A",
                "wavelength_nm": "nm", "anode_limit_a": "A", "pole_hz": "Hz"},
    applicability="no gain droop, no saturation physics, no transit-time spread; "
                  "unfiltered noisy log is grid-dependent exploration only",
    fidelity="L1", validation=["test_pmt_statistics", "test_pmt_chunk_independent",
                              "test_response_current_variance_matches_compound_poisson_theory"],
)  # fmt: skip


@dataclass(frozen=True)
class DetectorConfig:
    qe: float
    gain: float
    excess_noise_factor: float
    dark_current_a: float
    wavelength_nm: float
    anode_limit_a: float | None = None
    pole_hz: float | None = None  # optional fast-response pole, ASSUMED when used
    shot_noise: bool = True

    def __post_init__(self) -> None:
        if self.pole_hz is not None and (not np.isfinite(self.pole_hz) or self.pole_hz <= 0):
            raise ValueError("detector input-response pole must be positive and finite")


class DetectorResponse:
    """Explicit assumed pre-log node response to piecewise-constant charge-bin current.

    dy/dt = (I_bin - y)/tau, tau=1/(2*pi*fc). Starts at zero; prewarming is an
    experimental stage, never an implicit steady state of the first random bin.
    """

    def __init__(self, pole_hz: float, dt_s: float) -> None:
        if not np.all(np.isfinite([pole_hz, dt_s])) or min(pole_hz, dt_s) <= 0:
            raise ValueError("response frequency and time step must be positive and finite")
        rate = 2 * math.pi * pole_hz * dt_s
        self.b = [-math.expm1(-rate)]
        self.a = [1.0, -math.exp(-rate)]
        self.zi = np.zeros(1)

    def process(self, current: np.ndarray) -> np.ndarray:
        if not len(current):
            return np.array(current, dtype=float)
        result, self.zi = lfilter(self.b, self.a, current, zi=self.zi)
        return result

    def state(self) -> list[float]:
        return self.zi.tolist()

    def set_state(self, state: list[float]) -> None:
        value = np.asarray(state, dtype=float)
        if value.shape != (1,) or not np.all(np.isfinite(value)):
            raise ValueError("invalid input-response checkpoint state")
        self.zi = value.copy()


def mean_counts(cfg: DetectorConfig, power_w: np.ndarray, dt_s: float) -> np.ndarray:
    photon = H * C / (cfg.wavelength_nm * 1e-9)
    r_dark = cfg.dark_current_a / (E * cfg.gain)
    return (power_w * cfg.qe / photon + r_dark) * dt_s


def anode_current(cfg: DetectorConfig, power_w: np.ndarray, dt_s: float, rng: CounterRng,
                  domain: int, start_step: int):  # fmt: skip
    """Return (current in A, over-limit flag) for each step."""
    mu = mean_counts(cfg, power_w, dt_s)
    if not cfg.shot_noise:
        current = mu * E * cfg.gain / dt_s
    else:
        u1 = rng.uniform("pmt_poisson", domain, start_step, len(mu))
        n = poisson.ppf(u1, mu)
        if cfg.excess_noise_factor <= 1.0:
            q = n * cfg.gain
        else:
            u2 = rng.uniform("pmt_gamma", domain, start_step, len(mu))
            shape = np.where(n > 0, n / (cfg.excess_noise_factor - 1.0), 1.0)
            g = gamma_dist.ppf(u2, shape, scale=cfg.gain * (cfg.excess_noise_factor - 1.0))
            q = np.where(n > 0, g, 0.0)
        current = q * E / dt_s
    over = np.zeros(len(mu), dtype=bool)
    if cfg.anode_limit_a is not None:
        over = current > cfg.anode_limit_a
    return current, over
