"""ADC test-code sources (S2-T01): fixed, alternating, ramp, pseudo-random, from a file.

Each channel can carry a different pattern. The result is a ``ConvertedSamples``: the codes
plus everything needed to name a sample (``SampleId``) and to say when it was taken.
All codes are offset-binary unsigned integers of ``bits`` bits at this point; the output
coding of the real part is applied later by ``adc_link``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from zlscan.contracts.time import ClockDomain, SampleId, Tick


@dataclass(frozen=True)
class ConvertedSamples:
    codes: np.ndarray  # (n, len(channels)) uint16
    channels: tuple[int, ...]
    first_index: int
    epoch: int
    domain: ClockDomain
    bits: int = 16
    run_id: str = "run"
    branch_id: str = "main"
    session_id: int = 1
    meta: dict[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        return int(self.codes.shape[0])

    def sample_time(self, i: int) -> Tick:
        return self.domain.tick_of(self.first_index + i)

    def sample_times(self) -> np.ndarray:
        return np.array(
            [self.domain.tick_of(self.first_index + i) for i in range(len(self))], dtype=np.int64
        )

    def sample_id(self, i: int, channel_pos: int = 0) -> SampleId:
        return SampleId(
            self.run_id, self.branch_id, self.session_id, self.epoch, self.domain.id,
            self.channels[channel_pos], self.first_index + i,
        )  # fmt: skip


def _check_bits(code: int, bits: int) -> None:
    if not 0 <= code < 2**bits:
        raise ValueError(f"code {code} does not fit {bits} bits")


def fixed(code: int, n: int, bits: int = 16) -> np.ndarray:
    _check_bits(code, bits)
    return np.full(n, code, dtype=np.uint16)


def alternating(a: int, b: int, n: int, bits: int = 16) -> np.ndarray:
    _check_bits(a, bits)
    _check_bits(b, bits)
    out = np.empty(n, dtype=np.uint16)
    out[0::2], out[1::2] = a, b
    return out


def ramp(start: int, step: int, n: int, bits: int = 16) -> np.ndarray:
    """A ramp that wraps modulo 2**bits, like a counter."""
    _check_bits(start, bits)
    return ((start + step * np.arange(n, dtype=np.int64)) % 2**bits).astype(np.uint16)


def prbs(seed: int, n: int, bits: int = 16) -> np.ndarray:
    """Reproducible pseudo-random codes: the same seed gives the same sequence."""
    rng = np.random.Generator(np.random.PCG64(seed))
    return rng.integers(0, 2**bits, size=n, dtype=np.uint16)


def from_file(path: Path, n: int | None = None, bits: int = 16) -> np.ndarray:
    """Codes from a ``.npy`` file or a text file with one integer per line."""
    p = Path(path)
    arr = np.load(p) if p.suffix == ".npy" else np.loadtxt(p, dtype=np.int64, ndmin=1)
    if arr.ndim != 1:
        raise ValueError("a code file must hold a single column")
    if arr.size and (arr.min() < 0 or arr.max() >= 2**bits):
        raise ValueError(f"file holds codes outside 0..{2**bits - 1}")
    arr = arr.astype(np.uint16)
    if n is not None:
        if arr.size < n:
            raise ValueError(f"file holds {arr.size} codes, {n} requested")
        arr = arr[:n]
    return arr


def make_source(
    per_channel: dict[int, np.ndarray],
    domain: ClockDomain,
    first_index: int = 0,
    epoch: int = 0,
    bits: int = 16,
    **ids: Any,
) -> ConvertedSamples:
    lengths = {len(v) for v in per_channel.values()}
    if len(lengths) != 1:
        raise ValueError("all channels must have the same number of samples")
    channels = tuple(sorted(per_channel))
    codes = np.stack([per_channel[c] for c in channels], axis=1).astype(np.uint16)
    return ConvertedSamples(codes, channels, first_index, epoch, domain, bits, **ids)
