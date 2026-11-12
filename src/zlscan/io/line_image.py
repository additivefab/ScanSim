"""Minimal reconstruction (S2-T05): arrange codes into rows by sample identity, nothing else.

No radiometric or geometric correction. Rows come from the line number in the block header,
columns from the sample position inside the line. A line or sample that was not delivered stays
marked ``MISSING_SAMPLE``; later data never fills an earlier gap. The result also says whether
it is complete, so an early stop cannot look like success.

This module reads delivered data only: it must not import the simulation truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from zlscan.contracts.flags import InvalidFlag
from zlscan.io.rawstream import FLAG_LINE_START, ReadResult, read_all


@dataclass
class MinimalImage:
    codes: dict[int, np.ndarray]  # channel -> (lines, window_length) uint16
    invalid: np.ndarray  # (lines, window_length) uint16 bit flags
    first_line: int
    status: str  # "complete" | "incomplete"
    missing_lines: list[int] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def reconstruct(
    read: ReadResult | Path, expected_lines: range, window_length: int | None = None
) -> MinimalImage:
    if not isinstance(read, ReadResult):
        read = read_all(read)
    if read.session is None or read.fatal:
        return MinimalImage({}, np.zeros((0, 0), np.uint16), 0, "incomplete", [],
                            ["the raw file is unreadable"])  # fmt: skip
    wl = window_length or int(read.session.windows["window_length"])
    n = len(expected_lines)
    first = expected_lines.start
    channels = sorted({c for b in read.blocks for c in b.channels})
    codes = {c: np.zeros((n, wl), dtype=np.uint16) for c in channels}
    invalid = np.full((n, wl), int(InvalidFlag.MISSING_SAMPLE), dtype=np.uint16)

    segments = [(s.epoch, s.clock_domain, s.channels, s.first_sample_index, s.samples)
                for s in read.segments]  # fmt: skip
    reasons: list[str] = []
    for b in read.blocks:
        if not b.flags & FLAG_LINE_START or b.line_number not in expected_lines:
            continue
        row = b.line_number - first
        for epoch, domain, chans, seg_first, samples in segments:
            if epoch == b.epoch and domain == b.clock_domain and chans == b.channels:
                lo = b.first_sample_index - seg_first
                if 0 <= lo and lo + b.sample_count <= len(samples):
                    cnt = min(b.sample_count, wl)
                    for j, c in enumerate(chans):
                        codes[c][row, :cnt] = samples[lo : lo + cnt, j]
                    invalid[row, :cnt] = 0
                    if b.sample_count < wl:
                        reasons.append(f"line {b.line_number} is shorter than the window")
                    break
    got = {b.line_number for b in read.blocks}
    missing = [n_ for n_ in expected_lines if n_ not in got]
    if missing:
        reasons.append(f"{len(missing)} line(s) not delivered")
    if read.errors or read.losses:
        reasons.append("the raw file reports errors or losses")
    complete = not missing and not (invalid != 0).any() and not read.errors and not read.losses
    return MinimalImage(codes, invalid, first, "complete" if complete else "incomplete",
                        missing, reasons)  # fmt: skip
