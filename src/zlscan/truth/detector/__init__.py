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
    # Saturation candidate (spec 13.2), all ASSUMED and off when None.
    sat_current_a: float | None = None  # soft-knee current of the static compression
    droop_per_a: float | None = None  # steady-state fractional gain loss per ampere
    droop_tau_s: float | None = None  # build-up and recovery time constant of that loss

    def __post_init__(self) -> None:
        if self.pole_hz is not None and (not np.isfinite(self.pole_hz) or self.pole_hz <= 0):
            raise ValueError("detector input-response pole must be positive and finite")
        for value in (self.sat_current_a, self.droop_per_a, self.droop_tau_s):
            if value is not None and (not np.isfinite(value) or value <= 0):
                raise ValueError("saturation parameters must be positive and finite or null")
        if (self.droop_per_a is None) != (self.droop_tau_s is None):
            raise ValueError("gain droop needs both droop_per_a and droop_tau_s")

    @property
    def saturates(self) -> bool:
        return self.sat_current_a is not None or self.droop_per_a is not None


class Saturation:
    """Anode compression and gain droop with recovery (candidate, spec 13.2).

    I_c = I / (1 + I/I_sat)                     static soft knee
    dd/dt = (kappa * I_c - d) / tau             droop state, exact for bin-constant current
    I_out[k] = I_c[k] * max(0, 1 - d[k])        d[k] is the state at the start of bin k

    The state starts at zero and is never reset at a line start. It is not a divider-chain
    circuit model and no measured recovery curve supports its parameters yet.
    """

    def __init__(self, cfg: DetectorConfig, dt_s: float) -> None:
        if not cfg.saturates or not np.isfinite(dt_s) or dt_s <= 0:
            raise ValueError("saturation needs an enabled option and a positive time step")
        self.sat = cfg.sat_current_a
        self.kappa = cfg.droop_per_a
        self.a = math.exp(-dt_s / cfg.droop_tau_s) if cfg.droop_tau_s else None
        self.d = 0.0

    def process(self, current: np.ndarray) -> np.ndarray:
        x = np.asarray(current, dtype=float)
        if self.sat is not None:
            x = x / (1 + x / self.sat)
        if self.kappa is None or not len(x):
            return x
        ends, _ = lfilter([(1 - self.a) * self.kappa], [1.0, -self.a], x, zi=[self.a * self.d])
        starts = np.concatenate([[self.d], ends[:-1]])
        self.d = float(ends[-1])
        return x * np.maximum(0.0, 1 - starts)

    def state(self) -> list[float]:
        return [self.d]

    def set_state(self, state: list[float]) -> None:
        value = np.asarray(state, dtype=float)
        if value.shape != (1,) or not np.all(np.isfinite(value)) or value[0] < 0:
            raise ValueError("invalid saturation checkpoint state")
        self.d = float(value[0])


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
