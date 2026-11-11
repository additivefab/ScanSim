"""Multi-level min/max summaries of a waveform (docs/dev/08_workbench_spec.md; plan section 5.14).

Each level reduces blocks of samples to four numbers: minimum, maximum, first and last. Mean
down-sampling would erase a short pulse, a clipped peak or an overflow, so it is never used for
a display summary. Level 0 summarises blocks of ``base`` samples, level k blocks of
``base * factor**k``. Summaries are for display only; calibration, reconstruction and error
evaluation use the original samples.

Columns of a level: 0 = min, 1 = max, 2 = first sample of the block, 3 = last sample of the block.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def _reduce(x: np.ndarray, block: int) -> np.ndarray:
    """Summarise consecutive blocks of ``block`` rows; the last block may be shorter."""
    n = len(x)
    full = n // block
    parts = []
    if full:
        v = (
            x[: full * block, 0].reshape(full, block)
            if x.ndim == 2
            else x[: full * block].reshape(full, block)
        )
        parts.append(np.column_stack([v.min(axis=1), v.max(axis=1), v[:, 0], v[:, -1]]))
    if n % block:
        tail = x[full * block :, 0] if x.ndim == 2 else x[full * block :]
        parts.append(np.array([[tail.min(), tail.max(), tail[0], tail[-1]]], dtype=np.float32))
    return np.vstack(parts).astype(np.float32)


@dataclass
class Pyramid:
    base: int
    factor: int
    levels: list[np.ndarray]
    length: int

    @classmethod
    def build(
        cls, x: np.ndarray, base: int = 256, factor: int = 8, min_points: int = 1024
    ) -> Pyramid:
        x = np.asarray(x, dtype=np.float32)
        if not np.isfinite(x).all():
            raise ValueError("samples must be finite to be summarised")
        levels = [_reduce(x, base)]
        while len(levels[-1]) > min_points:
            prev = levels[-1]
            n = len(prev)
            full = n // factor
            parts = []
            if full:
                g = prev[: full * factor].reshape(full, factor, 4)
                parts.append(
                    np.column_stack([g[:, :, 0].min(1), g[:, :, 1].max(1), g[:, 0, 2], g[:, -1, 3]])
                )
            if n % factor:
                g = prev[full * factor :]
                parts.append(np.array([[g[:, 0].min(), g[:, 1].max(), g[0, 2], g[-1, 3]]]))
            levels.append(np.vstack(parts).astype(np.float32))
        return cls(base, factor, levels, len(x))

    def block_size(self, level: int) -> int:
        return self.base * self.factor**level

    def choose_level(self, span: int, pixel_width: int) -> int:
        """Coarsest level whose blocks are no larger than one pixel; -1 means "use raw samples"."""
        per_pixel = span / max(pixel_width, 1)
        best = -1
        for level in range(len(self.levels)):
            if self.block_size(level) <= per_pixel:
                best = level
        return best

    def query(
        self, start: int, end: int, pixel_width: int, raw: np.ndarray | None = None
    ) -> tuple[str, np.ndarray]:
        level = self.choose_level(end - start, pixel_width)
        if level < 0:
            if raw is None:
                raise LookupError("raw samples are not recorded for this window")
            return "raw", raw[start:end]
        size = self.block_size(level)
        lo, hi = start // size, -(-end // size)
        return "summary", self.levels[level][lo:hi]
