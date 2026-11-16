"""Observed feed-encoder counts: the reconstruction's row positions when an encoder exists.

The file is operator/instrument supplied and bound to one raw file by SHA-256. Nothing here
reads the simulation: the counts are an observation of the true feed, never ``v_y * t``.
Row ``k`` is placed at ``origin_y + (count[k] - count[0]) * pitch + pitch_y / 2``.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


class FeedEncoder(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    format: Literal["zlscan-feed-encoder/1"]
    raw_sha256: str
    pitch_m: float
    counts: list[int]
    origin: Literal["OBSERVED", "SIMULATED"]

    @model_validator(mode="after")
    def _valid(self) -> FeedEncoder:
        if not (math.isfinite(self.pitch_m) and self.pitch_m > 0):
            raise ValueError("encoder pitch must be positive and finite")
        if not self.counts:
            raise ValueError("encoder counts are empty")
        if any(b < a for a, b in zip(self.counts, self.counts[1:], strict=False)):
            raise ValueError("encoder counts must not decrease (lost or reversed counts)")
        return self

    def row_y_m(self, lines: int, pitch_y_m: float, origin_y_m: float) -> list[float]:
        if len(self.counts) != lines:
            raise ValueError("encoder needs exactly one count per captured line")
        first = self.counts[0]
        return [origin_y_m + (c - first) * self.pitch_m + 0.5 * pitch_y_m for c in self.counts]
