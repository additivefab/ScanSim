"""Trace recording policy (S3-T13): summaries for selected probes, raw samples only on request.

High-rate analogue signals are not stored by default. A selected probe is reduced to a
min/max/first/last pyramid as the run proceeds, so a coarse view still shows a one-step pulse,
a clipped peak or an overflow. Event indexes (here: the steps where a flag is set) are kept
separately so they can be found without scanning the waveform.
"""

from __future__ import annotations

import numpy as np

from zlscan.engine.chain import Chain, ChainConfig
from zlscan.trace.pyramid import Pyramid


def record(cfg: ChainConfig, steps: int, probes: set[str], chunk: int = 5000, base: int = 64,
           keep_raw: bool = False) -> dict:  # fmt: skip
    chain = Chain(cfg)
    chain.probe_names = set(probes)
    out = chain.run_steps(steps, chunk)
    result: dict = {"pyramids": {}, "raw": {}, "events": {}}
    for name, arr in out.probes.items():
        result["pyramids"][name] = Pyramid.build(arr.astype(np.float64), base=base)
        if keep_raw:
            result["raw"][name] = arr
    result["events"] = {
        "adc_saturated": np.flatnonzero(out.saturated).tolist(),
        "front_end_clipped": np.flatnonzero(out.front_end_clipped).tolist(),
        "pmt_over_limit": np.flatnonzero(out.over_limit).tolist(),
    }
    result["codes"] = out.codes
    return result
