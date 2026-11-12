"""Bounded delivered-only ADC display windows for one saved capture domain/channel."""

from __future__ import annotations

import zlib
from pathlib import Path

import numpy as np

from zlscan.io.rawstream import scan_blocks
from zlscan.trace.window import validate_window


class SavedADC:
    def __init__(self, path: Path, length: int, domain: int) -> None:
        self.path = path
        self.length = length
        read = scan_blocks(path, lambda *_: None)
        if (
            read.session is None
            or read.errors
            or read.losses
            or read.fatal
            or read.session.origin != "SIMULATED"
            or any(
                b.channels != [0] or b.epoch != 0 or b.clock_domain != domain for b in read.blocks
            )
        ):
            raise ValueError("saved workbench requires intact single-channel/single-domain capture")
        if any(b.first_sample_index + b.sample_count > length for b in read.blocks):
            raise ValueError("capture exceeds source sample span")
        self.blocks = read.blocks
        self.session = read.session
        stat = path.stat()
        self.identity = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)

    def unchanged(self) -> None:
        stat = self.path.stat()
        if self.identity != (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns):
            raise ValueError("saved capture changed after registration")

    def query(self, start: int, end: int, width: int) -> dict:
        validate_window(start, end, width)
        self.unchanged()
        start, end = max(0, start), min(self.length, end)
        if end <= start:
            return dict(status="not_recorded", values=[], reason="outside captured timeline")
        count = end - start
        block = 1 if count <= 2 * width else -(-count // width)
        size = -(-count // block)
        lows, highs, firsts, lasts = ([None] * size for _ in range(4))
        recorded = [0] * size
        with self.path.open("rb") as stream:
            for row in self.blocks:
                first = row.first_sample_index
                lo, hi = max(start, first), min(end, first + row.sample_count)
                if hi <= lo:
                    continue
                stream.seek(row.payload_offset)
                body = stream.read(row.sample_count * 2)
                if zlib.crc32(body) != row.payload_crc32:
                    raise ValueError("saved payload CRC mismatch")
                codes = np.frombuffer(body, dtype="<u2")
                for bucket in range((lo - start) // block, -(-(hi - start) // block)):
                    edge = start + bucket * block
                    a, b = max(lo, edge), min(hi, edge + block)
                    chunk = codes[a - first : b - first]
                    low, high = int(chunk.min()), int(chunk.max())
                    lows[bucket] = low if lows[bucket] is None else min(lows[bucket], low)
                    highs[bucket] = high if highs[bucket] is None else max(highs[bucket], high)
                    if a == edge:
                        firsts[bucket] = int(chunk[0])
                    if b == min(end, edge + block):
                        lasts[bucket] = int(chunk[-1])
                    recorded[bucket] += b - a
        self.unchanged()
        return dict(
            status="exact_record" if block == 1 else "summary_only",
            level="raw" if block == 1 else "display_bins",
            start=start,
            block_size=block,
            source_count=count,
            support_start=start,
            support_end=end,
            boundary_extra=False,
            values=firsts
            if block == 1
            else [
                [lows[i], highs[i], firsts[i], lasts[i], min(block, count - i * block)]
                for i in range(size)
            ],
            columns=["min", "max", "first", "last", "count"],
            recorded_counts=recorded,
            missing_count=count - sum(recorded),
            visibility="OBSERVABLE",
            source="delivered ZLRAW only; gaps include unkept source positions",
        )
