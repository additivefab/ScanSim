"""Dual-channel physical acquisition with line-sized payloads and a frame-wide queue schedule."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from zlscan.adc.patterns import ConvertedSamples
from zlscan.adc_link.link import LinkConfig, receive, transmit
from zlscan.contracts.calibration import Binding, CaptureDescription
from zlscan.contracts.time import ClockDomain
from zlscan.digital.acquisition import (
    AcquisitionConfig,
    capture_windows,
    channel_mask,
    session_header,
)
from zlscan.engine.chain import Chain, ChainConfig
from zlscan.engine.physical_run import line_steps, simulation_binding
from zlscan.engine.validity import physical_applicability
from zlscan.io.manifest import sha256_file
from zlscan.io.rawstream import FLAG_EPOCH_START, FLAG_LINE_END, FLAG_LINE_START, RawWriter
from zlscan.io.stream_image import stream_code_image
from zlscan.transport.queues import Item, StageSpec, simulate
from zlscan.truth.scan import sync_ticks


@dataclass(frozen=True)
class Window:
    line: int
    start: int
    stop: int


@dataclass
class KernelTiming:
    seconds: float = 0.0


def _payloads(
    chains: tuple[Chain, Chain],
    steps_per_line: int,
    lines: int,
    windows: list[Window],
    timing: KernelTiming,
) -> Iterator[tuple[Window, np.ndarray]]:
    retained = np.empty((0, 2), dtype=np.uint16)
    remaining = iter(windows)
    window = next(remaining, None)
    for _ in range(lines):
        started = time.perf_counter()
        a, b = (chain.advance(steps_per_line) for chain in chains)
        timing.seconds += time.perf_counter() - started
        if a.first_sample != b.first_sample or a.codes.shape != b.codes.shape:
            raise ValueError("dual physical channels produced different sample identities")
        first = a.first_sample - len(retained)
        available = np.concatenate((retained, np.column_stack((a.codes, b.codes))))
        end = first + len(available)
        while window is not None and window.stop <= end:
            yield window, available[window.start - first : window.stop - first]
            window = next(remaining, None)
        keep = end if window is None else window.start
        retained = available[max(0, keep - first) :]
    if window is not None:
        raise RuntimeError("planned capture window was not produced")


def stream_physical(
    out: Path,
    primary: ChainConfig,
    secondary: ChainConfig,
    lines: int,
    *,
    acquisition: AcquisitionConfig | None = None,
    stages: list[StageSpec] | None = None,
    link: LinkConfig | None = None,
) -> dict:
    """Run two analogue chains on one clock and reread verified rows to a disk-backed image."""
    if lines < 1:
        raise ValueError("physical run requires at least one line")
    if (primary.fs_hz, primary.os, primary.domain_id, primary.adc.bits, primary.scan) != (
        secondary.fs_hz,
        secondary.os,
        secondary.domain_id,
        secondary.adc.bits,
        secondary.scan,
    ) or (primary.scan.row_jitter_s and primary.seed != secondary.seed):
        raise ValueError("dual physical channels require common scan timing and ADC sampling")
    step_count = line_steps(primary)
    if step_count <= 0:
        raise ValueError("physical line has no fine steps")
    channels = (0, 1)
    dom = ClockDomain(primary.domain_id, primary.fs_hz)
    acquisition = acquisition or AcquisitionConfig(
        round(primary.scan.window_start_s() * primary.fs_hz),
        round(primary.scan.active_s * primary.fs_hz),
    )
    if stages is None:
        stages = [
            StageSpec("rx", 10**7, 1e9),
            StageSpec("net", 10**8, 1e9),
            StageSpec("disk", 10**8, 1e9),
        ]
    if not stages:
        raise ValueError("dual streaming requires at least one transport stage")
    link = link or LinkConfig(bits=primary.adc.bits, latency_half_cycles=24, propagation_ps=1500)
    if link.overflow_offset_cycles:
        raise ValueError("dual streaming requires an aligned overflow signal")
    start_wall = time.perf_counter()
    chains = Chain(primary), Chain(secondary)
    sync = sync_ticks(primary.scan, lines, chains[0].rng, primary.domain_id)
    capture_end = -(-(step_count * lines) // primary.os)
    windows = [
        Window(line, first, min(stop, capture_end))
        for line, (_, _, first, stop) in enumerate(capture_windows(dom, sync, acquisition))
        if first < capture_end
    ]

    # Transport decisions depend on timing and packet size only, not the ADC values.
    # Compute them before materializing any payload so queues never retain frame arrays.
    def arrival(window: Window) -> int:
        tail = ConvertedSamples(
            np.zeros((1, 2), np.uint16), channels, window.stop - 1, 0, dom, primary.adc.bits
        )
        return max(dom.tick_of(window.stop), int(transmit(tail, link).capture_time[0]))

    items = [
        Item(
            w.line,
            (w.stop - w.start) * 4 + 64,
            0,
            dom.id,
            w.start,
            w.stop - w.start,
            arrival(w),
            w.line,
        )
        for w in windows
    ]
    transport = simulate(items, stages)
    if not transport.conserved(items):
        raise RuntimeError("dual streaming transport did not conserve the planned windows")
    delivered = {item.item_id for item, _ in transport.delivered}
    losses = [{**asdict(loss), "channels": list(channels)} for loss in transport.losses]
    losses.extend(
        {
            "layer": "transport",
            "reason": "in_flight_after_drain",
            "item_id": item.item_id,
            "epoch": item.epoch,
            "clock_domain": item.clock_domain,
            "channels": list(channels),
            "first_sample_index": item.first_sample_index,
            "last_sample_index": item.first_sample_index + item.sample_count - 1,
        }
        for item in transport.in_flight
    )

    observable = out / "observable"
    observable.mkdir(parents=True, exist_ok=False)
    raw_path = observable / "raw.zlraw"
    src = ConvertedSamples(np.empty((0, 2), np.uint16), channels, 0, 0, dom, primary.adc.bits)
    written = 0
    kernel = KernelTiming()
    with RawWriter(raw_path, session_header(src, acquisition, 1)) as writer:
        for window, payload in _payloads(chains, step_count, lines, windows, kernel):
            if window.line not in delivered:
                continue
            chunk = ConvertedSamples(payload, channels, window.start, 0, dom, primary.adc.bits)
            captured = receive(transmit(chunk, link), link)
            writer.write_block(
                epoch=0,
                channel_mask=channel_mask(channels),
                clock_domain=dom.id,
                first_sample_index=captured.first_index,
                samples=captured.codes,
                flags=FLAG_LINE_START | FLAG_LINE_END | (FLAG_EPOCH_START if written == 0 else 0),
                line_number=window.line,
            )
            written += 1
    if written != len(delivered):
        raise RuntimeError("dual streaming lost a scheduled payload")
    (observable / "losses.json").write_text(json.dumps(losses, indent=2) + "\n", encoding="utf-8")
    image = stream_code_image(
        raw_path, observable, lines, acquisition.window_length, channels, losses
    )
    identities = [simulation_binding(config).simulation_hash for config in (primary, secondary)]
    digest = hashlib.sha256(json.dumps([identities, asdict(acquisition), asdict(link)]).encode())
    capture = CaptureDescription(
        raw_sha256=sha256_file(raw_path),
        lines=lines,
        binding=Binding(simulation_hash=digest.hexdigest(), channel_roles={"0": None, "1": None}),
    )
    (observable / "capture.json").write_text(
        capture.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    run = {
        "mode": "physical_stream",
        "lines": lines,
        "channels": list(channels),
        "fine_steps_per_channel": step_count * lines,
        "retained_samples_per_channel": sum(i.sample_count for i, _ in transport.delivered),
        "integrity": image["status"],
        "recon_status": image["status"],
        "lost_blocks": len(losses),
        "planned_blocks": len(items),
        "written_blocks": written,
        "queue_peak_bytes": {stat.name: stat.peak_bytes for stat in transport.stats},
        "config_hashes": [primary.hash(), secondary.hash()],
        "physical_applicability": [
            physical_applicability(primary),
            physical_applicability(secondary),
        ],
        "kernel_s": kernel.seconds,
        "wall_s": time.perf_counter() - start_wall,
    }
    (out / "run.json").write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
    return run
