"""Integer time (docs/dev/03_contracts.md, section 2) and sample identity (section 3).

Global time is an integer number of picoseconds (``Tick``). A periodic event is identified by its
count within a clock domain and converted to a tick with exact integer arithmetic, so no
rounding error accumulates however many periods pass: ``tick_of(n)`` is within one picosecond
of the true instant for every n, and successive periods are the floor or the ceiling of the true
period. Floating-point seconds are never used as a time key.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import NamedTuple

PS_PER_S = 10**12

Tick = int  # picoseconds, 64-bit range is ample (about +/- 9.2e6 s)


@dataclass(frozen=True)
class ClockDomain:
    """A clock of ``freq_num / freq_den`` hertz whose count 0 falls on ``phase_ticks``."""

    id: int
    freq_num: int
    freq_den: int = 1
    phase_ticks: int = 0

    def __post_init__(self) -> None:
        if self.freq_num <= 0 or self.freq_den <= 0:
            raise ValueError("clock frequency must be positive")

    @property
    def _numerator(self) -> int:  # period in ps is _numerator / freq_num
        return PS_PER_S * self.freq_den

    def tick_of(self, count: int) -> Tick:
        """Instant of the ``count``-th edge: floor of the exact value, plus the phase."""
        return (count * self._numerator) // self.freq_num + self.phase_ticks

    def count_at(self, tick: Tick) -> int:
        """Index of the latest edge at or before ``tick`` (-1 before the first edge)."""
        t = tick - self.phase_ticks
        return -((-(t + 1) * self.freq_num) // self._numerator) - 1


class PhaseAccumulator:
    """Incremental form of ``ClockDomain.tick_of`` using only integer addition.

    It carries the integer quotient and remainder of ``count * period``, which is what a hardware
    phase accumulator does; it always equals the closed form, and ``jump`` reaches any count in
    one step. Its state is three integers, small enough to live in a checkpoint.
    """

    def __init__(self, domain: ClockDomain, count: int = 0) -> None:
        self.domain = domain
        self.count = count
        self._q, self._r = divmod(count * domain._numerator, domain.freq_num)

    def step(self) -> Tick:
        d = self.domain
        self.count += 1
        step_q, step_r = divmod(d._numerator, d.freq_num)
        self._q += step_q
        self._r += step_r
        if self._r >= d.freq_num:
            self._r -= d.freq_num
            self._q += 1
        return self._q + d.phase_ticks

    def jump(self, count: int) -> Tick:
        self.count = count
        self._q, self._r = divmod(count * self.domain._numerator, self.domain.freq_num)
        return self._q + self.domain.phase_ticks

    def checkpoint(self) -> bytes:
        return json.dumps([self.count, self._q, self._r]).encode()

    @classmethod
    def restore(cls, domain: ClockDomain, blob: bytes) -> PhaseAccumulator:
        count, q, r = json.loads(blob)
        acc = cls(domain)
        acc.count, acc._q, acc._r = count, q, r
        return acc


class SampleId(NamedTuple):
    """Unique identity of one source sample. A row or facet number is not an identity."""

    run_id: str
    branch_id: str
    session_id: int
    epoch: int
    clock_domain: int
    channel: int
    sample_index: int
