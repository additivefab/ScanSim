"""ADC conversion (S3-T09): sample the fine waveform at the ADC edges, jitter, quantise.

    LSB = (Vmax - Vmin) / 2^B     code = clip(floor((V - Vmin) / LSB), 0, 2^B - 1)

Saturation is its own flag (the voltage left the range), not "code equals an end value".
Jitter moves the sampling instant by ``d``; the value is taken from the backward slope,
``V(t + d) ~ V(t) + d dV/dt``, which needs only already computed samples. The result is fed to the
stage-2 output link by ``engine.chain``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from zlscan.contracts.module import ModuleSpec

SPEC = ModuleSpec(
    name="truth.adc", interface={"V": "V", "code": "1"}, state={"last": "V"},
    equation="code = clip(floor((V-Vmin)/LSB), 0, 2^B-1); saturated = V<Vmin or V>=Vmax",
    parameters={"bits": "1", "vmin": "V", "vmax": "V", "jitter_rms_s": "s"},
    applicability="ideal converter; gain/offset/INL/thermal noise are stage 5",
    fidelity="L1", validation=["test_adc_quantisation", "test_adc_saturation_flag"],
)  # fmt: skip


@dataclass(frozen=True)
class AdcConfig:
    bits: int
    vmin: float
    vmax: float
    jitter_rms_s: float = 0.0

    @property
    def lsb(self) -> float:
        return (self.vmax - self.vmin) / 2**self.bits


def quantize(cfg: AdcConfig, v: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    saturated = (v < cfg.vmin) | (v >= cfg.vmax)
    codes = np.clip(np.floor((v - cfg.vmin) / cfg.lsb), 0, 2**cfg.bits - 1).astype(np.uint16)
    return codes, saturated


def sample_with_jitter(v_at: np.ndarray, v_prev: np.ndarray, dt_s: float,
                       jitter_s: np.ndarray) -> np.ndarray:  # fmt: skip
    return v_at + jitter_s * (v_at - v_prev) / dt_s
