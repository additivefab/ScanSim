"""``zlscan run --mode physical`` (S3-T09, S3-T11): the forward chain feeding the digital chain.

``run_physical`` runs the chain for a number of lines, hands the ADC codes and the physical line
sync instants to the stage-2 pipeline and writes the same run directory as the digital mode, plus
the simulation truth separately. ``stream_raw`` is the streaming kernel used for performance
scenarios: it keeps only a couple of lines in memory and writes ZLRAW blocks as lines complete,
so its memory use does not grow with the image height.
"""

from __future__ import annotations

import time
import tracemalloc
from pathlib import Path
from typing import Any

import numpy as np

from zlscan.adc.patterns import ConvertedSamples
from zlscan.contracts.time import ClockDomain
from zlscan.digital.acquisition import AcquisitionConfig, channel_mask, session_header
from zlscan.engine.chain import Chain, ChainConfig
from zlscan.engine.digital_run import run_from_source
from zlscan.io.rawstream import FLAG_EPOCH_START, FLAG_LINE_END, FLAG_LINE_START, RawWriter
from zlscan.truth import scan as scan_mod


def line_steps(cfg: ChainConfig) -> int:
    return round(cfg.scan.line_period_s * cfg.fs_hz * cfg.os)


def run_physical(out: Path, cfg: ChainConfig, lines: int, digital: dict[str, Any] | None = None,
                 chunk_lines: float = 1.0) -> dict[str, Any]:  # fmt: skip
    chain = Chain(cfg)
    total = line_steps(cfg) * lines
    chunk = max(1000, int(line_steps(cfg) * chunk_lines))
    res = chain.run_steps(total, chunk)
    dom = ClockDomain(cfg.domain_id, cfg.fs_hz)
    src = ConvertedSamples(res.codes.reshape(-1, 1), (0,), res.first_sample, 0, dom,
                           cfg.adc.bits, meta={"saturated": res.saturated})  # fmt: skip
    syncs = scan_mod.sync_ticks(cfg.scan, lines, chain.rng, cfg.domain_id)
    spp = max(1, round(cfg.fs_hz * cfg.scan.pitch_x_m / cfg.scan.v_x))
    window_cycles = round(cfg.scan.active_s * cfg.fs_hz)
    start_cycles = round(cfg.scan.window_start_s() * cfg.fs_hz)
    dcfg = {
        "window_start": start_cycles, "window_length": window_cycles, "mode_name": "physical",
        "stages": [{"name": "rx", "capacity_bytes": 10**7, "rate_Bps": 1e9},
                   {"name": "net", "capacity_bytes": 10**8, "rate_Bps": 1e9}],
        "seed": cfg.seed, **(digital or {}),
    }  # fmt: skip
    extra = {
        "samples_per_pixel_estimate": spp,
        "evidence": cfg.evidence,
        "saturated_samples": int(res.saturated.sum()),
        "front_end_clipped_samples": int(res.front_end_clipped.sum()),
        "pmt_over_limit_samples": int(res.over_limit.sum()),
        "model_version": "chain-0.1",
        "config_hash": cfg.hash(),
    }
    return run_from_source(out, src, syncs, dcfg, preset="physical", extra_run=extra)


def stream_raw(path: Path, cfg: ChainConfig, lines: int, acq: AcquisitionConfig | None = None,
               measure_memory: bool = False) -> dict[str, Any]:  # fmt: skip
    """Run ``lines`` lines and write one ZLRAW block per line without keeping the image."""
    chain = Chain(cfg)
    dom = ClockDomain(cfg.domain_id, cfg.fs_hz)
    steps = line_steps(cfg)
    acq = acq or AcquisitionConfig(
        round(cfg.scan.window_start_s() * cfg.fs_hz), round(cfg.scan.active_s * cfg.fs_hz)
    )
    syncs = scan_mod.sync_ticks(cfg.scan, lines, chain.rng, cfg.domain_id)
    starts = []
    for t in syncs:
        c = dom.count_at(t)
        c0 = (c if dom.tick_of(c) == t else c + 1) + acq.sync_latency_cycles
        starts.append(c0 + acq.window_start)
    src = ConvertedSamples(np.zeros((0, 1), np.uint16), (0,), 0, 0, dom, cfg.adc.bits)
    if measure_memory:
        tracemalloc.start()
    t0 = time.perf_counter()
    buf = np.zeros(0, dtype=np.uint16)
    buf_first = 0
    next_line = 0
    written = 0
    with RawWriter(path, session_header(src, acq, 1)) as w:
        for _ in range(lines + 1):
            r = chain.advance(steps)
            buf = np.concatenate([buf, r.codes])
            while next_line < lines:
                s = starts[next_line]
                e = s + acq.window_length
                if e > buf_first + len(buf):
                    break
                seg = buf[s - buf_first : e - buf_first].reshape(-1, 1)
                w.write_block(epoch=0, channel_mask=channel_mask((0,)), clock_domain=cfg.domain_id,
                              first_sample_index=s, samples=seg, line_number=next_line,
                              flags=FLAG_LINE_START | FLAG_LINE_END
                              | (FLAG_EPOCH_START if written == 0 else 0))  # fmt: skip
                written += 1
                next_line += 1
            keep_from = starts[next_line] if next_line < lines else buf_first + len(buf)
            drop = max(0, keep_from - buf_first)
            buf, buf_first = buf[drop:], buf_first + drop
    wall = time.perf_counter() - t0
    peak = None
    if measure_memory:
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
    done = chain.step
    return {
        "lines_requested": lines, "lines_written": written, "fine_steps": done,
        "wall_s": wall, "steps_per_s": done / wall if wall else None,
        "python_peak_bytes": peak,
    }  # fmt: skip
