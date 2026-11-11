"""Control state machine and epochs (S2-T08).

A command has three separate moments: requested, effective, read back. A sample is bound to the
configuration that was effective at its *sampling instant*; samples still in the pipeline when
the configuration changes keep the epoch of the configuration they were taken under. A reset in
any state starts a new epoch and marks the epoch it interrupted as incomplete.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import numpy as np

from zlscan.adc.patterns import ConvertedSamples
from zlscan.contracts.time import Tick


class State(StrEnum):
    UNCONFIGURED = "unconfigured"
    CONFIGURED = "configured"
    SETTLING = "settling"
    CALIBRATING = "calibrating"
    READY = "ready"
    SCANNING = "scanning"
    DRAINING = "draining"
    DONE = "done"
    FAULT = "fault"
    ABORTED = "aborted"


ALLOWED: dict[State, set[State]] = {
    State.UNCONFIGURED: {State.CONFIGURED},
    State.CONFIGURED: {State.SETTLING, State.UNCONFIGURED},
    State.SETTLING: {State.CALIBRATING, State.READY},
    State.CALIBRATING: {State.READY},
    State.READY: {State.SCANNING, State.CONFIGURED},
    State.SCANNING: {State.DRAINING},
    State.DRAINING: {State.DONE},
    State.DONE: {State.CONFIGURED, State.UNCONFIGURED},
    State.FAULT: {State.UNCONFIGURED},
    State.ABORTED: {State.UNCONFIGURED},
}
ANY_TIME = {State.FAULT, State.ABORTED}


class TransitionError(ValueError):
    pass


@dataclass
class Epoch:
    id: int
    effective_tick: Tick
    config: dict[str, Any]
    reset: bool = False
    incomplete: bool = False  # interrupted by a reset or abort


@dataclass
class ControlLog:
    kind: str  # "request" | "effective" | "readback" | "reset" | "state"
    tick: Tick
    detail: dict[str, Any] = field(default_factory=dict)


class Controller:
    def __init__(self) -> None:
        self.state = State.UNCONFIGURED
        self.epochs: list[Epoch] = [Epoch(0, 0, {})]
        self.log: list[ControlLog] = []

    def to(self, new: State, tick: Tick) -> None:
        if new not in ANY_TIME and new not in ALLOWED[self.state]:
            raise TransitionError(f"{self.state.value} -> {new.value} is not allowed")
        self.log.append(ControlLog("state", tick, {"from": self.state.value, "to": new.value}))
        if new in ANY_TIME:
            self.epochs[-1].incomplete = True
        self.state = new

    def request(self, key: str, value: Any, request_tick: Tick, effective_tick: Tick,
                readback_tick: Tick | None = None, readback_value: Any = None) -> None:  # fmt: skip
        """Change a setting. Effective must not precede the request; readback is optional."""
        if effective_tick < request_tick:
            raise ValueError("a command cannot take effect before it is requested")
        self.log.append(ControlLog("request", request_tick, {"key": key, "value": value}))
        self.log.append(ControlLog("effective", effective_tick, {"key": key, "value": value}))
        if readback_tick is not None:
            self.log.append(ControlLog("readback", readback_tick,
                                       {"key": key, "value": readback_value}))  # fmt: skip
        last = self.epochs[-1]
        if effective_tick < last.effective_tick:
            raise ValueError("configuration changes must be applied in time order")
        self.epochs.append(Epoch(last.id + 1, effective_tick, {**last.config, key: value}))

    def reset(self, tick: Tick) -> None:
        """A reset in any state: a new epoch, and the interrupted one is marked incomplete."""
        self.log.append(ControlLog("reset", tick, {"from": self.state.value}))
        interrupted = self.state in (
            State.SCANNING,
            State.DRAINING,
            State.SETTLING,
            State.CALIBRATING,
        )
        if interrupted:
            self.epochs[-1].incomplete = True
        last = self.epochs[-1]
        self.epochs.append(Epoch(last.id + 1, max(tick, last.effective_tick), {}, reset=True))
        self.state = State.UNCONFIGURED

    def epoch_at(self, sample_tick: Tick) -> Epoch:
        chosen = self.epochs[0]
        for e in self.epochs:
            if e.effective_tick <= sample_tick:
                chosen = e
        return chosen

    def readback_confirmed(self, key: str) -> bool:
        requested = [x for x in self.log if x.kind == "request" and x.detail["key"] == key]
        if not requested:
            return False
        want = requested[-1].detail["value"]
        reads = [x for x in self.log if x.kind == "readback" and x.detail["key"] == key]
        return bool(reads) and reads[-1].detail["value"] == want


def split_by_epoch(src: ConvertedSamples, ctl: Controller) -> list[ConvertedSamples]:
    """Cut the samples where the effective configuration changed (by sampling instant)."""
    times = src.sample_times()
    ids = np.array([ctl.epoch_at(int(t)).id for t in times])
    out: list[ConvertedSamples] = []
    start = 0
    for i in range(1, len(ids) + 1):
        if i == len(ids) or ids[i] != ids[start]:
            out.append(
                ConvertedSamples(
                    src.codes[start:i], src.channels, src.first_index + start, int(ids[start]),
                    src.domain, src.bits, src.run_id, src.branch_id, src.session_id, dict(src.meta),
                )
            )  # fmt: skip
            start = i
    return out
