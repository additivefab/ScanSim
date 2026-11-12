"""Control state machine and epochs (S2-T08).

A command has three separate moments: requested, effective, read back. A sample is bound to the
configuration that was effective at its *sampling instant*; samples still in the pipeline when
the configuration changes keep the epoch of the configuration they were taken under. A reset in
any state starts a new epoch and marks the epoch it interrupted as incomplete.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, dataclass, field
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
        self._next_command_id = 0
        self._last_action_tick: Tick | None = None

    FORMAT = "zlscan-control-checkpoint/1"

    @staticmethod
    def _tick(value: Tick) -> None:
        if type(value) is not int or not -(2**63) <= value < 2**63:
            raise ValueError("control time must be a signed 64-bit integer Tick")

    def _action_tick(self, tick: Tick) -> None:
        self._tick(tick)
        if self._last_action_tick is not None and tick < self._last_action_tick:
            raise ValueError("control actions must be declared in time order")

    @staticmethod
    def _copy_value(value: Any) -> Any:
        def validate(item):
            if isinstance(item, dict):
                if any(not isinstance(key, str) for key in item):
                    raise ValueError("control configuration keys must be strings")
                for child in item.values():
                    validate(child)
            elif isinstance(item, (list, tuple)):
                for child in item:
                    validate(child)

        validate(value)
        try:
            return json.loads(json.dumps(value, allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise ValueError("control configuration must contain finite JSON values") from exc

    def to(self, new: State, tick: Tick) -> None:
        self._action_tick(tick)
        new = State(new)
        if new not in ANY_TIME and new not in ALLOWED[self.state]:
            raise TransitionError(f"{self.state.value} -> {new.value} is not allowed")
        self.log.append(ControlLog("state", tick, {"from": self.state.value, "to": new.value}))
        if new in ANY_TIME:
            self.epoch_at(tick).incomplete = True
        self.state = new
        self._last_action_tick = tick

    def request(self, key: str, value: Any, request_tick: Tick, effective_tick: Tick,
                readback_tick: Tick | None = None, readback_value: Any = None) -> None:  # fmt: skip
        """Change a setting. Effective must not precede the request; readback is optional."""
        self._action_tick(request_tick)
        self._tick(effective_tick)
        if not isinstance(key, str) or not key:
            raise ValueError("control key must be a nonempty string")
        if effective_tick < request_tick:
            raise ValueError("a command cannot take effect before it is requested")
        last = self.epochs[-1]
        if effective_tick < last.effective_tick:
            raise ValueError("configuration changes must be applied in time order")
        if readback_tick is not None:
            self._tick(readback_tick)
            if readback_tick < effective_tick:
                raise ValueError("a readback cannot confirm a command before it takes effect")
        value = self._copy_value(value)
        readback_value = self._copy_value(readback_value) if readback_tick is not None else None
        command = str(self._next_command_id)
        detail = {"key": key, "value": value, "command_id": command}
        entries = [
            ControlLog("request", request_tick, deepcopy(detail)),
            ControlLog("effective", effective_tick, deepcopy(detail)),
        ]
        if readback_tick is not None:
            entries.append(
                ControlLog(
                    "readback",
                    readback_tick,
                    {"key": key, "value": readback_value, "command_id": command},
                )
            )
        config = {**deepcopy(last.config), key: deepcopy(value)}
        self.log.extend(entries)
        self.epochs.append(Epoch(last.id + 1, effective_tick, config))
        self._next_command_id += 1
        self._last_action_tick = request_tick

    def reset(self, tick: Tick) -> None:
        """Reset the logical controller without inventing hardware-specific reset effects."""
        self._action_tick(tick)
        last = self.epochs[-1]
        if tick < last.effective_tick:
            raise ValueError("reset cannot precede an already declared configuration epoch")
        self.log.append(ControlLog("reset", tick, {"from": self.state.value}))
        interrupted = self.state in (
            State.SCANNING,
            State.DRAINING,
            State.SETTLING,
            State.CALIBRATING,
        )
        if interrupted:
            last.incomplete = True
        self.epochs.append(Epoch(last.id + 1, tick, {}, reset=True))
        self.state = State.UNCONFIGURED
        self._last_action_tick = tick

    def epoch_at(self, sample_tick: Tick) -> Epoch:
        chosen = self.epochs[0]
        for e in self.epochs:
            if e.effective_tick <= sample_tick:
                chosen = e
        return chosen

    def readback_confirmed(self, key: str, *, at_tick: Tick | None = None) -> bool:
        """Query declared records; without a cutoff this includes declared future readbacks."""
        if at_tick is not None:
            self._tick(at_tick)
        available = [x for x in self.log if at_tick is None or x.tick <= at_tick]
        requested = [x for x in available if x.kind == "request" and x.detail["key"] == key]
        if not requested:
            return False
        request = requested[-1]
        position = self.log.index(request)
        if any(
            x.kind == "reset" and (at_tick is None or x.tick <= at_tick)
            for x in self.log[position + 1 :]
        ):
            return False
        reads = [
            x
            for x in available
            if x.kind == "readback" and x.detail["command_id"] == request.detail["command_id"]
        ]
        return bool(reads) and reads[-1].detail["value"] == request.detail["value"]

    @staticmethod
    def _hash(value: dict) -> str:
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, allow_nan=False).encode()
        ).hexdigest()

    def _snapshot(self) -> dict:
        return {
            "format": self.FORMAT,
            "state": self.state.value,
            "epochs": [{**asdict(e), "effective_tick": str(e.effective_tick)} for e in self.epochs],
            "log": [{**asdict(row), "tick": str(row.tick)} for row in self.log],
            "log_cursor": str(len(self.log)),
            "next_command_id": str(self._next_command_id),
            "last_action_tick": str(self._last_action_tick)
            if self._last_action_tick is not None
            else None,
        }

    @classmethod
    def _replay(cls, value: dict) -> Controller:
        control = cls()
        rows = value["log"]
        i = 0
        while i < len(rows):
            row = rows[i]
            detail, tick = row["detail"], int(row["tick"])
            if row["kind"] == "state":
                control.to(State(detail["to"]), tick)
            elif row["kind"] == "reset":
                control.reset(tick)
            elif row["kind"] == "request":
                i += 1
                effective = rows[i]
                if effective["kind"] != "effective" or effective["detail"] != detail:
                    raise ValueError("control effective record does not match its request")
                readback = (
                    rows[i + 1] if i + 1 < len(rows) and rows[i + 1]["kind"] == "readback" else None
                )
                control.request(
                    detail["key"],
                    detail["value"],
                    tick,
                    int(effective["tick"]),
                    int(readback["tick"]) if readback else None,
                    readback["detail"]["value"] if readback else None,
                )
                i += int(readback is not None)
            else:
                raise ValueError("unsupported control log record")
            i += 1
        if control._snapshot() != value:
            raise ValueError(
                "control state, epochs or log cursor disagree with the command history"
            )
        return control

    def checkpoint(self) -> bytes:
        value = self._snapshot()
        self._replay(value)
        value["checksum"] = self._hash(value)
        return json.dumps(value, sort_keys=True, allow_nan=False).encode()

    @classmethod
    def restore(cls, blob: bytes) -> Controller:
        value = json.loads(blob)
        if not isinstance(value, dict) or value.get("format") != cls.FORMAT:
            raise ValueError("control checkpoint version is unsupported")
        if value.pop("checksum", None) != cls._hash(value):
            raise ValueError("control checkpoint checksum differs")
        try:
            return cls._replay(value)
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("control checkpoint history is malformed") from exc


def split_by_epoch(src: ConvertedSamples, ctl: Controller) -> list[ConvertedSamples]:
    """Cut the samples where the effective configuration changed (by sampling instant)."""
    for value in src.meta.values():
        if isinstance(value, np.ndarray) and (value.ndim == 0 or len(value) != len(src)):
            raise ValueError("per-sample metadata must match the source length")
    times = src.sample_times()
    ids = np.array([ctl.epoch_at(int(t)).id for t in times])
    out: list[ConvertedSamples] = []
    start = 0
    for i in range(1, len(ids) + 1):
        if i == len(ids) or ids[i] != ids[start]:
            out.append(
                ConvertedSamples(
                    src.codes[start:i], src.channels, src.first_index + start, int(ids[start]),
                    src.domain, src.bits, src.run_id, src.branch_id, src.session_id,
                    {
                        key: value[start:i].copy()
                        if isinstance(value, np.ndarray) else deepcopy(value)
                        for key, value in src.meta.items()
                    },
                )
            )  # fmt: skip
            start = i
    return out
