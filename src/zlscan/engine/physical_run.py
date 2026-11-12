"""``zlscan run --mode physical`` (S3-T09, S3-T11): the forward chain feeding the digital chain.

``run_physical`` runs the chain for a number of lines, hands the ADC codes and the physical line
sync instants to the stage-2 pipeline and writes the same run directory as the digital mode, plus
the simulation truth separately. ``stream_raw`` is the streaming kernel used for performance
scenarios: it keeps only a couple of lines in memory and writes ZLRAW blocks as lines complete,
so it does not keep frame-sized sample arrays. Sync scheduling uses O(lines) metadata.
"""

from __future__ import annotations

import time
import tracemalloc
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np

from zlscan.adc.patterns import ConvertedSamples
from zlscan.contracts.calibration import Binding, CaptureDescription
from zlscan.contracts.time import ClockDomain
from zlscan.digital.acquisition import (
    AcquisitionConfig,
    capture_windows,
    channel_mask,
    session_header,
)
from zlscan.engine.chain import Chain, ChainConfig
from zlscan.engine.digital_run import run_from_source
from zlscan.io.manifest import sha256_file
from zlscan.io.rawstream import FLAG_EPOCH_START, FLAG_LINE_END, FLAG_LINE_START, RawWriter
from zlscan.replay.replay import run_with_checkpoints
from zlscan.truth import scan as scan_mod


def simulation_binding(config: ChainConfig) -> Binding:
    """Hash the simulated acquisition settings independently of the target and noise realization."""
    identity = replace(config, seed=0, evidence={}, scenes={}, timeline=[], injections=[]).hash()
    return Binding(simulation_hash=identity, channel_roles={"0": None})


def line_steps(cfg: ChainConfig) -> int:
    return round(cfg.scan.line_period_s * cfg.fs_hz * cfg.os)


def run_physical(
    out: Path,
    cfg: ChainConfig,
    lines: int,
    digital: dict[str, Any] | None = None,
    chunk_lines: float = 1.0,
    *,
    save_checkpoints: bool = False,
) -> dict[str, Any]:
    if lines < 1:
        raise ValueError("physical run requires at least one line")
    chain = Chain(cfg)
    total = line_steps(cfg) * lines
    chunk = max(1000, int(line_steps(cfg) * chunk_lines))
    if save_checkpoints:
        res, checkpoints = run_with_checkpoints(cfg, total, line_steps(cfg), chunk)
    else:
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
    extra["fine_steps"] = total
    run = run_from_source(out, src, syncs, dcfg, preset="physical", extra_run=extra)
    observable = out / "observable"
    capture = CaptureDescription(
        binding=simulation_binding(cfg),
        raw_sha256=sha256_file(observable / "raw.zlraw"),
        lines=lines,
    )
    (observable / "capture.json").write_text(
        capture.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    if save_checkpoints:
        directory = out / "truth" / "checkpoints"
        directory.mkdir(exist_ok=True)
        for step, blob in checkpoints.blobs.items():
            (directory / f"{step}.json").write_bytes(blob)
    return run


def stream_raw(path: Path, cfg: ChainConfig, lines: int, acq: AcquisitionConfig | None = None,
               measure_memory: bool = False) -> dict[str, Any]:  # fmt: skip
    """Write accepted capture windows to ZLRAW without keeping the frame samples."""
    chain = Chain(cfg)
    dom = ClockDomain(cfg.domain_id, cfg.fs_hz)
    steps = line_steps(cfg)
    acq = acq or AcquisitionConfig(
        round(cfg.scan.window_start_s() * cfg.fs_hz), round(cfg.scan.active_s * cfg.fs_hz)
    )
    syncs = scan_mod.sync_ticks(cfg.scan, lines, chain.rng, cfg.domain_id)
    windows = enumerate(capture_windows(dom, syncs, acq))
    pending = next(windows, None)
    capture_end = -(-(steps * lines) // cfg.os)
    src = ConvertedSamples(np.zeros((0, 1), np.uint16), (0,), 0, 0, dom, cfg.adc.bits)
    if measure_memory:
        tracemalloc.start()
    t0 = time.perf_counter()
    buf = np.zeros(0, dtype=np.uint16)
    written = 0
    try:
        with RawWriter(path, session_header(src, acq, 1)) as w:
            for _ in range(lines):
                r = chain.advance(steps)
                # The retained tail ends at this chunk's first sample, even if a
                # gap between capture windows left the tail empty.
                buf_first = r.first_sample - len(buf)
                buf = np.concatenate([buf, r.codes])
                available_end = buf_first + len(buf)
                while pending is not None:
                    line_number, (_, _, start, stop) = pending
                    if start >= capture_end:
                        pending = None
                        break
                    stop = min(stop, capture_end)
                    if stop > available_end:
                        break
                    segment = buf[start - buf_first : stop - buf_first].reshape(-1, 1)
                    w.write_block(
                        epoch=0,
                        channel_mask=channel_mask(src.channels),
                        clock_domain=dom.id,
                        first_sample_index=start,
                        samples=segment,
                        line_number=line_number,
                        flags=FLAG_LINE_START
                        | FLAG_LINE_END
                        | (FLAG_EPOCH_START if written == 0 else 0),
                    )
                    written += 1
                    pending = next(windows, None)
                keep_from = pending[1][2] if pending is not None else available_end
                buf = buf[max(0, keep_from - buf_first) :]
    finally:
        peak = tracemalloc.get_traced_memory()[1] if measure_memory else None
        if measure_memory:
            tracemalloc.stop()
    wall = time.perf_counter() - t0
    done = chain.step
    return {
        "lines_requested": lines, "lines_written": written, "fine_steps": done,
        "wall_s": wall, "steps_per_s": done / wall if wall else None,
        "python_peak_bytes": peak,
    }  # fmt: skip
