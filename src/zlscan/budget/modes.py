"""Acquisition modes and per-stage backlog (fluid model).

``continuous``: the input never stops, so every stage needs an average service rate (stalls
included) strictly above the input, plus buffer for the longest stall.
``finite_frame``: a burst of known duration; stages may be slower than the input if the buffer
holds the surplus and the backlog drains within a limit.
``periodic_burst``: finite bursts repeating; the backlog must be gone before the next burst.

Service that finds nothing to do is lost (never banked as credit). Unknown stage numbers give
UNRESOLVED, never a guess.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum

from zlscan.contracts.verdict import Verdict

MAX_STEPS = 300_000
MIN_DT = 1e-5


class AcquisitionMode(StrEnum):
    CONTINUOUS = "continuous"
    FINITE_FRAME = "finite_frame"
    PERIODIC_BURST = "periodic_burst"


@dataclass(frozen=True)
class Stage:
    name: str
    capacity_bytes: float | None
    service_Bps: float | None
    stalls: tuple[tuple[float, float], ...] = ()  # (start_s, duration_s) windows with no service


@dataclass
class StageResult:
    name: str
    peak_backlog_bytes: float
    overflow: bool
    last_busy_s: float | None = None
    busy_at_end: bool = False


@dataclass
class Assessment:
    verdict: Verdict
    reason: str
    stages: list[StageResult] = field(default_factory=list)
    drain_time_s: float | None = None


def simulate_stage_backlogs(
    input_Bps: float,
    duration_s: float,
    stages: list[Stage],
    start_s: float = 0.0,
    horizon_s: float | None = None,
) -> list[StageResult]:
    horizon = horizon_s if horizon_s is not None else start_s + duration_s * 2
    dt = max(MIN_DT, horizon / MAX_STEPS)
    n = int(math.ceil(horizon / dt))
    backlog = [0.0] * len(stages)
    peak = [0.0] * len(stages)
    last_busy: list[float | None] = [None] * len(stages)
    for k in range(n):
        t = (k + 0.5) * dt
        flow = input_Bps * dt if start_s <= t < start_s + duration_s else 0.0
        for i, st in enumerate(stages):
            stalled = any(s <= t < s + d for s, d in st.stalls)
            capacity_step = 0.0 if stalled else (st.service_Bps or 0.0) * dt
            backlog[i] += flow
            out = min(backlog[i], capacity_step)
            backlog[i] -= out
            if backlog[i] > peak[i]:
                peak[i] = backlog[i]
            if backlog[i] > 1e-9:
                last_busy[i] = (k + 1) * dt
            flow = out
    results = []
    for i, st in enumerate(stages):
        over = st.capacity_bytes is not None and peak[i] > st.capacity_bytes
        results.append(StageResult(st.name, peak[i], over, last_busy[i], backlog[i] > 1e-9))
    return results


def _unresolved(stages: list[Stage], need_capacity: bool) -> str | None:
    for st in stages:
        if st.service_Bps is None:
            return f"stage {st.name}: service rate unknown"
        if need_capacity and st.capacity_bytes is None:
            return f"stage {st.name}: buffer capacity unknown"
    return None


def _stall_fraction(st: Stage, span_s: float) -> float:
    total = 0.0
    for s, d in st.stalls:
        total += max(0.0, min(s + d, span_s) - max(s, 0.0))
    return min(1.0, total / span_s)


def _drain(results: list[StageResult], end_s: float) -> float:
    if any(r.busy_at_end for r in results):
        return math.inf
    busy = [r.last_busy_s for r in results if r.last_busy_s is not None]
    return max(0.0, max(busy) - end_s) if busy else 0.0


def assess(
    mode: AcquisitionMode,
    input_Bps: float,
    stages: list[Stage],
    duration_s: float | None = None,
    drain_limit_s: float | None = None,
    period_s: float | None = None,
) -> Assessment:
    if input_Bps <= 0:
        raise ValueError("input_Bps must be positive")
    if mode is AcquisitionMode.CONTINUOUS:
        span = duration_s if duration_s is not None else 1.0
        has_stalls = any(s.stalls for s in stages)
        why = _unresolved(stages, need_capacity=has_stalls)
        if why:
            return Assessment(Verdict.UNRESOLVED, why)
        for st in stages:
            eff = st.service_Bps * (1 - _stall_fraction(st, span))
            if input_Bps >= eff:
                return Assessment(
                    Verdict.FAIL,
                    f"average input {input_Bps:.6g} B/s is not below stage {st.name} "
                    f"effective service {eff:.6g} B/s",
                )
        results = simulate_stage_backlogs(input_Bps, span, stages, 0.0, span)
        bad = [r.name for r in results if r.overflow]
        if bad:
            return Assessment(Verdict.FAIL, f"buffer overflow in {', '.join(bad)}", results)
        return Assessment(Verdict.PASS, "average rate and stall buffering hold", results)

    if duration_s is None or duration_s <= 0:
        raise ValueError("duration_s is required for burst modes")
    why = _unresolved(stages, need_capacity=True)
    if why:
        return Assessment(Verdict.UNRESOLVED, why)
    if mode is AcquisitionMode.FINITE_FRAME:
        horizon = duration_s + (drain_limit_s if drain_limit_s is not None else duration_s * 10)
        horizon += max(1e-3, duration_s * 0.2)
    else:
        if period_s is None or period_s <= duration_s:
            raise ValueError("period_s must exceed duration_s")
        horizon = period_s * 2
    results = simulate_stage_backlogs(input_Bps, duration_s, stages, 0.0, horizon)
    bad = [r.name for r in results if r.overflow]
    if bad:
        return Assessment(Verdict.FAIL, f"buffer overflow in {', '.join(bad)}", results)
    drain = _drain(results, duration_s)
    if mode is AcquisitionMode.FINITE_FRAME:
        if drain_limit_s is not None and drain > drain_limit_s:
            return Assessment(
                Verdict.FAIL, f"cannot drain within {drain_limit_s} s (needs {drain:.6g} s)",
                results, drain,
            )  # fmt: skip
        return Assessment(Verdict.PASS, "buffers hold and the backlog drains", results, drain)
    if drain > period_s - duration_s:
        return Assessment(
            Verdict.FAIL,
            f"backlog needs {drain:.6g} s to drain; the next burst arrives after "
            f"{period_s - duration_s:.6g} s on a non-empty buffer",
            results, drain,
        )  # fmt: skip
    return Assessment(Verdict.PASS, "backlog drains before the next burst", results, drain)


def output_rate(
    item_rate_hz: float,
    item_bytes: int,
    channels: int = 1,
    flag_bytes: int = 0,
    overhead_fraction: float = 0.0,
    aggregation: str | None = None,
    value_domain: str | None = None,
    front_end: str | None = None,
) -> float:
    """Bytes per second of an output stream, counted from items, not from a raw byte rate."""
    if item_rate_hz <= 0 or item_bytes <= 0 or channels <= 0 or flag_bytes < 0:
        raise ValueError("rates and widths must be positive")
    if aggregation == "mean" and value_domain == "raw_code" and front_end == "log":
        raise ValueError("averaging raw log-amplifier codes is refused: convert to linear first")
    return item_rate_hz * (item_bytes * channels + flag_bytes) * (1 + overhead_fraction)
