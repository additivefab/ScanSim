"""Data-path arithmetic: average rates, frame sizes and queue accounting.

``bandwidth`` is the *continuous-mode* average-rate gate (docs/dev/04_physics_spec.md, section
10): long-run input must stay below the output, with no margin when equal. The other two
acquisition modes (``finite_frame`` and ``periodic_burst``) arrive with S1-T04; a single
unmodelled average must not be used to reject or accept a finite-frame capture.

Rates are in bytes per second. A PASS here only means "average-rate margin", never that bursts,
stalls or the hardware have been checked.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum


class GateResult(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True)
class Bandwidth:
    raw_bytes_per_s: float
    kept_bytes_per_s: float
    transmitted_bytes_per_s: float
    output_bytes_per_s: float | None
    average_gate: GateResult
    note: str = (
        "PASS only means average-rate margin, not burst or hardware verification; "
        "equality has no margin."
    )


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _positive_finite(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a positive finite number")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return float(value)


def bandwidth(
    fs_hz: float,
    channels: int,
    stored_bytes: int = 2,
    keep_fraction: float = 1.0,
    decimation: int = 1,
    overhead_fraction: float = 0.0,
    output_bytes_per_s: float | None = None,
) -> Bandwidth:
    fs = _positive_finite(fs_hz, "fs_hz")
    _positive_int(channels, "channels")
    _positive_int(stored_bytes, "stored_bytes")
    _positive_int(decimation, "decimation")
    if not math.isfinite(keep_fraction) or not 0 <= keep_fraction <= 1:
        raise ValueError("keep_fraction must be in [0, 1]")
    if not math.isfinite(overhead_fraction) or overhead_fraction < 0:
        raise ValueError("overhead_fraction must be non-negative")
    raw = fs * channels * stored_bytes
    kept = raw * keep_fraction / decimation
    transmitted = kept * (1 + overhead_fraction)
    limit = None if output_bytes_per_s is None else _positive_finite(output_bytes_per_s, "output")
    if limit is None:
        gate = GateResult.UNRESOLVED
    else:
        gate = GateResult.PASS if transmitted < limit else GateResult.FAIL
    return Bandwidth(raw, kept, transmitted, limit, gate)


def frame_bytes(width: int, height: int, channels: int, stored_bytes: int) -> int:
    for name, value in (
        ("width", width),
        ("height", height),
        ("channels", channels),
        ("stored_bytes", stored_bytes),
    ):
        _positive_int(value, name)
    return width * height * channels * stored_bytes


def queue_trace(events: list[tuple[str, int]], capacity: int) -> dict[str, int]:
    """Replay an ordered trace of arrivals and services through one finite queue.

    Service capacity that finds the queue empty is lost, not banked for later. On overflow the
    newest arrivals are dropped and counted. Conservation always holds:
    produced == consumed + lost + queued.
    """
    if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 0:
        raise ValueError("capacity must be a non-negative integer")
    queued = peak = produced = consumed = lost = 0
    for kind, amount in events:
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise ValueError("event amount must be a non-negative integer")
        if kind == "arrival":
            produced += amount
            accepted = min(amount, capacity - queued)
            queued += accepted
            lost += amount - accepted
            peak = max(peak, queued)
        elif kind == "service":
            sent = min(queued, amount)
            queued -= sent
            consumed += sent
        else:
            raise ValueError(f"unknown event kind: {kind}")
    if produced != consumed + lost + queued:
        raise RuntimeError("queue accounting violated conservation")
    return {
        "produced": produced,
        "consumed": consumed,
        "lost": lost,
        "queued": queued,
        "peak": peak,
    }
