"""Board memory budget: do the frame buffers that must coexist fit in DDR next to the reserve?

All frames that exist at the same time count (raw, reconstructed, float copies, each with its
number of buffers and a per-frame overhead fraction). An unknown reserve gives UNRESOLVED.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from zlscan.contracts.verdict import Verdict


@dataclass(frozen=True)
class FrameSpec:
    name: str
    nx: int
    ny: int
    channels: int
    bytes_per_sample: int
    buffers: int = 1
    overhead_fraction: float = 0.0

    @property
    def bytes(self) -> float:
        base = self.nx * self.ny * self.channels * self.bytes_per_sample * self.buffers
        return base * (1 + self.overhead_fraction)


@dataclass
class MemoryBudget:
    verdict: Verdict
    reason: str
    total_bytes: float
    per_frame: dict[str, float] = field(default_factory=dict)
    free_bytes: float | None = None


def board_memory(
    frames: list[FrameSpec], reserved_bytes: float | None, ddr_bytes: float
) -> MemoryBudget:
    if ddr_bytes <= 0:
        raise ValueError("ddr_bytes must be positive")
    per = {f.name: f.bytes for f in frames}
    total = sum(per.values())
    if reserved_bytes is None:
        return MemoryBudget(Verdict.UNRESOLVED, "reserved memory is unknown", total, per)
    free = ddr_bytes - total - reserved_bytes
    if free < 0:
        return MemoryBudget(
            Verdict.FAIL, f"frames + reserve exceed DDR by {-free:.0f} bytes", total, per, free
        )
    return MemoryBudget(Verdict.PASS, f"{free:.0f} bytes free", total, per, free)
