"""Causality helpers (S3-T15): sample lineage and linear dependency windows.

For a cascade of first-order poles the impulse response of the slowest pole bounds the tail, so
``dependency_steps`` returns a step count after which an input change has decayed below
``eps`` of its size. It is a truncation bound with a stated error, not an exact support.
"""

from __future__ import annotations

import math
from typing import Any


def dependency_steps(poles_hz: list[float], dt_s: float, eps: float = 1e-9) -> int:
    """Steps after which a cascade of first-order poles has settled to within ``eps``."""
    if not poles_hz or eps <= 0 or eps >= 1:
        raise ValueError("need at least one pole and 0 < eps < 1")
    taus = [1 / (2 * math.pi * f) for f in poles_hz]
    # a cascade of n poles decays no slower than exp(-t / tau_max) times a polynomial of degree n-1
    t = max(taus) * (math.log(1 / eps) + len(taus) * math.log(1 + len(taus)) + len(taus))
    return math.ceil(t / dt_s) + 1


def sample_lineage(out: Any, i: int, queue_path: list[str], block: int | None) -> dict:
    return {
        "sample_index": out.first_index + i,
        "sample_time": int(out.sample_time[i]),
        "adc_output_time": int(out.adc_output_time[i]),
        "pin_arrival_time": int(out.pin_arrival_time[i]),
        "capture_time": int(out.capture_time[i]),
        "queue_path": queue_path,
        "block": block,
    }
