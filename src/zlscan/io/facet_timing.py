"""Validate observable line timing and derive instrument facet phase."""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, StrictInt, model_validator

from zlscan.contracts.events import Derivation, Event
from zlscan.contracts.time import ClockDomain
from zlscan.contracts.verdict import Verdict
from zlscan.io.rawstream import FLAG_LINE_START, ReadResult


def infer_facet_phase(
    sync_ticks: np.ndarray,
    facets: int,
    period_ticks: int,
    tolerance_ticks: int,
    *,
    anchors: Mapping[int, int] | None = None,
) -> np.ndarray:
    """Use indexed reference observations to lock; never infer phase across a missing sync."""
    ticks = np.asarray(sync_ticks)
    if (
        ticks.ndim != 1
        or not np.issubdtype(ticks.dtype, np.signedinteger)
        or np.any(np.diff(ticks) <= 0)
        or facets < 1
        or period_ticks <= 0
        or not 0 <= tolerance_ticks < period_ticks / 2
    ):
        raise ValueError("invalid line synchronization observations or tracking parameters")
    anchors = anchors or {}
    if any(
        not 0 <= index < len(ticks) or not 0 <= phase < facets for index, phase in anchors.items()
    ):
        raise ValueError("facet anchor lies outside the capture or facet range")
    phases = np.full(len(ticks), -1, dtype=np.int64)
    for index, tick in enumerate(ticks):
        if index in anchors:
            phases[index] = anchors[index]
        elif index and phases[index - 1] >= 0:
            gap = tick - ticks[index - 1]
            if abs(gap - period_ticks) <= tolerance_ticks:
                phases[index] = (phases[index - 1] + 1) % facets
    return phases


class TimingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    facets: StrictInt = Field(ge=1)
    period_ticks: StrictInt = Field(gt=0)
    tolerance_ticks: StrictInt = Field(ge=0)
    phase_reference: str = Field(min_length=1)


class LineTiming(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line: StrictInt = Field(ge=0)
    sync: Event
    anchor: Event | None = None


class FacetTiming(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw_sha256: str
    policy: TimingPolicy
    lines: list[LineTiming]


class FacetMap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x_coefficients_m: tuple[FiniteFloat, FiniteFloat, FiniteFloat]
    x_residual_rms_m: FiniteFloat = Field(ge=0)
    support_u: tuple[FiniteFloat, FiniteFloat]
    y_offset_m: FiniteFloat | None

    @model_validator(mode="after")
    def _monotonic_support(self) -> FacetMap:
        low, high = self.support_u
        if not -1 <= low < high <= 1:
            raise ValueError("facet calibration support is outside the normalized scan window")
        _, b, c = self.x_coefficients_m
        nearest_zero = min(max(0, low), high)
        if min(b + 3 * c * z**2 for z in (low, high, nearest_zero)) <= 0:
            raise ValueError("facet calibration folds the forward scan")
        return self


class GeometryModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: TimingPolicy
    window: dict
    facets: dict[str, FacetMap]

    @model_validator(mode="after")
    def _valid_facets(self) -> GeometryModel:
        if not self.facets or set(self.facets) - {str(i) for i in range(self.policy.facets)}:
            raise ValueError("facet calibration contains invalid phase groups")
        return self


def observed_phases(
    timing: FacetTiming, raw: ReadResult, raw_sha256: str, n_lines: int
) -> np.ndarray:
    """Require measured sync identity at each declared line; reset lock across gaps or epochs."""
    session = raw.session
    if session is None or timing.raw_sha256 != raw_sha256:
        raise ValueError("facet timing does not belong to this raw capture")
    try:
        clocks = {entry["id"]: ClockDomain(**entry) for entry in session.clock_domains}
        latency = session.windows["sync_latency_cycles"] + session.windows["window_start"]
    except (KeyError, TypeError) as exc:
        raise ValueError("capture lacks facet timing clock or sample window metadata") from exc
    blocks = {b.line_number: b for b in raw.blocks if b.flags & FLAG_LINE_START}
    observations = sorted(timing.lines, key=lambda entry: entry.line)
    phases = np.full(n_lines, -1, dtype=np.int64)
    policy = timing.policy
    if 2 * policy.tolerance_ticks >= policy.period_ticks:
        raise ValueError("facet sync tolerance must be below half the line period")
    previous = None
    previous_phase = -1
    for entry in observations:
        line, sync, anchor = entry.line, entry.sync, entry.anchor
        block = blocks.get(line)
        if block is None or line >= n_lines or (previous and line <= previous.line):
            raise ValueError("facet timing requires unique delivered line identities")
        for event, kind in ((sync, "line_sync"), (anchor, "facet_index")):
            if event is None:
                continue
            if (
                event.kind != kind
                or event.recon_verdict() is not Verdict.PASS
                or event.derivation is not Derivation.SENSOR
                or event.origin.value != session.origin
                or (event.session_id, event.epoch, event.clock_domain)
                != (session.session_id, block.epoch, block.clock_domain)
            ):
                raise ValueError("facet timing requires observable sensor events from this line")
        clock = clocks.get(block.clock_domain)
        if clock is None:
            raise ValueError("facet line has no declared sample clock")
        first_edge = clock.count_at(sync.tick)
        if clock.tick_of(first_edge) < sync.tick:
            first_edge += 1
        if first_edge + latency != block.first_sample_index:
            raise ValueError("line sync tick disagrees with the acquired sample window")
        if previous and sync.tick <= previous.sync.tick:
            raise ValueError("line sync ticks must be increasing")

        phase = -1
        if anchor is not None:
            phase = anchor.payload.get("phase")
            if (
                anchor.capability_ref != policy.phase_reference
                or anchor.tick != sync.tick
                or type(phase) is not int
                or not 0 <= phase < policy.facets
            ):
                raise ValueError("invalid observable facet anchor")
        elif (
            previous
            and previous_phase >= 0
            and line == previous.line + 1
            and (sync.epoch, sync.clock_domain) == (previous.sync.epoch, previous.sync.clock_domain)
            and abs(sync.tick - previous.sync.tick - policy.period_ticks) <= policy.tolerance_ticks
        ):
            phase = (previous_phase + 1) % policy.facets
        phases[line] = phase
        previous, previous_phase = entry, phase
    return phases
