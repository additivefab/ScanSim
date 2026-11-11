"""First-order low-pass with the exact recursion for piecewise-linear input (physics spec 5).

    u = 2 pi fc dt;  a = exp(-u);  r = (1 - a) / u
    y[k] = a y[k-1] + (r - a) x[k-1] + (1 - r) x[k]

State is the ``lfilter`` delay line, so the filter can be advanced in any chunking and
checkpointed. A filter starts in the steady state of its first input (no start-up transient from
an arbitrary zero), unless a state is given.
"""

from __future__ import annotations

import math

import numpy as np
from scipy.signal import lfilter


def coefficients(fc_hz: float, dt_s: float) -> tuple[np.ndarray, np.ndarray]:
    if fc_hz <= 0 or dt_s <= 0:
        raise ValueError("cut-off frequency and step must be positive")
    u = 2 * math.pi * fc_hz * dt_s
    a = math.exp(-u)
    r = (1 - a) / u
    return np.array([1 - r, r - a]), np.array([1.0, -a])


class FirstOrder:
    def __init__(self, fc_hz: float, dt_s: float) -> None:
        self.b, self.a = coefficients(fc_hz, dt_s)
        self.zi: np.ndarray | None = None

    def process(self, x: np.ndarray) -> np.ndarray:
        x = np.asarray(x, dtype=float)
        if x.size == 0:
            return x.copy()
        if self.zi is None:  # steady state of a constant input equal to the first sample (gain 1)
            self.zi = np.array([(self.b[1] - self.a[1]) * x[0]])
        y, self.zi = lfilter(self.b, self.a, x, zi=self.zi)
        return y

    def state(self) -> list[float] | None:
        return None if self.zi is None else [float(v) for v in self.zi]

    def set_state(self, zi: list[float] | None) -> None:
        self.zi = None if zi is None else np.array(zi, dtype=float)


def reference_response(fc_hz: float, dt_s: float, x: np.ndarray) -> np.ndarray:
    """Plain loop of the recursion, used by tests as the definition."""
    u = 2 * math.pi * fc_hz * dt_s
    a = math.exp(-u)
    r = (1 - a) / u
    y = np.empty_like(x, dtype=float)
    y[0] = x[0]
    for k in range(1, len(x)):
        y[k] = a * y[k - 1] + (r - a) * x[k - 1] + (1 - r) * x[k]
    return y
