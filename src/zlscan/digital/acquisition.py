"""Digital acquisition reference model (S2-T03). Integer arithmetic only.

Line sync is a physical instant (integer picoseconds) with no relation to the ADC clock. It is
synchronised into the ADC clock domain: the first ADC edge at or after the instant, plus the
synchroniser latency. From that edge a line counter runs; samples ``window_start`` to
``window_start + window_length - 1`` of the line are kept. Tags are derived from the same sample
index as the data, so tag and data cannot drift apart. A sync that arrives while a line is open
is ignored and counted; a missing sync simply produces no line (the interval check in
``integrity`` sees the gap).

Aggregation is off by default. Averaging raw log-amplifier codes is refused.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

import numpy as np

from zlscan.adc.patterns import ConvertedSamples
from zlscan.contracts.time import ClockDomain, Tick
from zlscan.io.rawstream import (
    FLAG_EPOCH_START,
    FLAG_LINE_END,
    FLAG_LINE_START,
    RawWriter,
    SessionHeader,
)


@dataclass(frozen=True)
class AcquisitionConfig:
    window_start: int  # samples after the synchronised line start
    window_length: int
    sync_latency_cycles: int = 2  # two-flop synchroniser
    aggregation: str | None = None
    value_domain: str = "raw_code"
    front_end: str = "unknown"
    frozen_lut: str | None = None  # required for aggregation
    number_format: str | None = None  # required for aggregation


class AcquisitionConfigError(ValueError):
    pass


def validate(cfg: AcquisitionConfig) -> None:
    if cfg.window_start < 0 or cfg.window_length <= 0:
        raise AcquisitionConfigError("window_start must be >= 0 and window_length > 0")
    if cfg.sync_latency_cycles < 0:
        raise AcquisitionConfigError("sync_latency_cycles must be >= 0")
    if cfg.aggregation is not None:
        if cfg.value_domain != "linear":
            raise AcquisitionConfigError(
                "aggregating raw codes is refused (a log front end must be linearised first)"
            )
        if not cfg.frozen_lut or not cfg.number_format:
            raise AcquisitionConfigError(
                "aggregation needs a frozen lookup table and a number format"
            )


@dataclass
class Line:
    line_number: int
    sync_tick: Tick
    start_index: int  # sample index of the first kept sample
    counter_zero_index: int  # sample index where the line counter was 0
    phase_ps: int  # sync instant to the ADC edge that took it
    samples: np.ndarray  # (window_length, channels)
    truncated: bool = False


@dataclass
class AcquisitionResult:
    lines: list[Line] = field(default_factory=list)
    ignored_syncs: list[Tick] = field(default_factory=list)
    skipped_lines: list[tuple[int, str]] = field(default_factory=list)


def _first_edge_at_or_after(domain: ClockDomain, tick: Tick) -> int:
    c = domain.count_at(tick)
    return c if domain.tick_of(c) == tick else c + 1


def capture_windows(
    domain: ClockDomain,
    sync_ticks: list[Tick],
    cfg: AcquisitionConfig,
    ignored_syncs: list[Tick] | None = None,
) -> Iterator[tuple[Tick, int, int, int]]:
    """Yield the sync, counter zero and half-open sample range of each accepted window."""
    validate(cfg)
    if cfg.aggregation is not None:
        raise NotImplementedError("board-side aggregation is validated but not implemented yet")
    busy_until = 0  # exclusive
    for tick in sorted(sync_ticks):
        counter_zero = _first_edge_at_or_after(domain, tick) + cfg.sync_latency_cycles
        if counter_zero < busy_until:
            if ignored_syncs is not None:
                ignored_syncs.append(tick)
            continue
        start = counter_zero + cfg.window_start
        busy_until = start + cfg.window_length
        yield tick, counter_zero, start, busy_until


def acquire(
    src: ConvertedSamples,
    sync_ticks: list[Tick],
    cfg: AcquisitionConfig,
    first_line_number: int = 0,
) -> AcquisitionResult:
    d = src.domain
    res = AcquisitionResult()
    capture_end = src.first_index + len(src)
    windows = capture_windows(d, sync_ticks, cfg, res.ignored_syncs)
    for line_no, (t, c0, start, end) in enumerate(windows, first_line_number):
        if not src.first_index <= start < capture_end:
            res.skipped_lines.append((line_no, "window outside the captured samples"))
            continue
        stop = min(end, capture_end)
        rows = src.codes[start - src.first_index : stop - src.first_index]
        res.lines.append(
            Line(line_no, t, start, c0, d.tick_of(c0 - cfg.sync_latency_cycles) - t, rows,
                 truncated=stop < end)
        )  # fmt: skip
    return res


def session_header(src: ConvertedSamples, cfg: AcquisitionConfig, session_id: int) -> SessionHeader:
    return SessionHeader(
        schema_version="0.1", session_id=session_id, created_at="1970-01-01T00:00:00Z",
        hardware_hash=None, firmware_hash=None, wiring_rev=None, register_dump_hash=None,
        calibration_id=None,
        channel_roles={str(c): None for c in src.channels},
        clock_domains=[{"id": src.domain.id, "freq_num": src.domain.freq_num,
                        "freq_den": src.domain.freq_den, "phase_ticks": src.domain.phase_ticks}],
        coding="offset_binary", bits_per_sample=src.bits, bytes_per_sample=2,
        windows={"window_start": cfg.window_start, "window_length": cfg.window_length,
                 "sync_latency_cycles": cfg.sync_latency_cycles},
        origin="SIMULATED",
    )  # fmt: skip


def channel_mask(channels: tuple[int, ...]) -> int:
    mask = 0
    for c in channels:
        mask |= 1 << c
    return mask


def block_args(src: ConvertedSamples, line: Line, first: bool) -> dict:
    flags = FLAG_LINE_START | FLAG_LINE_END | (FLAG_EPOCH_START if first else 0)
    return {
        "epoch": src.epoch, "channel_mask": channel_mask(src.channels),
        "clock_domain": src.domain.id, "first_sample_index": line.start_index,
        "samples": line.samples, "flags": flags, "line_number": line.line_number,
    }  # fmt: skip


def write_raw(path, src: ConvertedSamples, result: AcquisitionResult, cfg: AcquisitionConfig,
              session_id: int = 1) -> None:  # fmt: skip
    with RawWriter(path, session_header(src, cfg, session_id)) as w:
        for i, line in enumerate(result.lines):
            w.write_block(**block_args(src, line, first=i == 0))
