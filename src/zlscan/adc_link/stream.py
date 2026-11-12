"""Incremental functional ADC link with in-flight words and checkpointable overflow alignment."""

from __future__ import annotations

import hashlib
import json
from collections import deque
from copy import deepcopy
from dataclasses import asdict, dataclass, replace

import numpy as np

from zlscan.adc.patterns import ConvertedSamples
from zlscan.adc_link.link import LinkConfig, LinkOutput, check_config, transmit
from zlscan.contracts.time import ClockDomain

ARRAYS = (
    "words",
    "overflow",
    "overflow_valid",
    "sample_time",
    "adc_output_time",
    "pin_arrival_time",
    "capture_time",
)
TIMES = ARRAYS[3:]


@dataclass
class _Pending:
    output: LinkOutput
    known: np.ndarray


def _slice(output: LinkOutput, start: int, end: int) -> LinkOutput:
    return replace(
        output,
        first_index=output.first_index + start,
        **{key: getattr(output, key)[start:end].copy() for key in ARRAYS},
    )


def _encode(output: LinkOutput) -> dict:
    return {
        "first_index": str(output.first_index),
        "epoch": output.epoch,
        "run_id": output.run_id,
        "branch_id": output.branch_id,
        "session_id": output.session_id,
        **{key: getattr(output, key).tolist() for key in ARRAYS[:3]},
        **{key: [str(int(tick)) for tick in getattr(output, key)] for key in TIMES},
    }


class StreamLink:
    FORMAT = "zlscan-link-checkpoint/1"

    def __init__(
        self,
        domain: ClockDomain,
        config: LinkConfig,
        channels: tuple[int, ...],
        mode: str = "research",
    ) -> None:
        check_config(config, mode)
        if config.latency_half_cycles is None or config.propagation_ps is None:
            raise ValueError("incremental link requires explicit pipeline and propagation delays")
        if not channels or len(set(channels)) != len(channels):
            raise ValueError("link source channels must be nonempty and unique")
        if config.channel_order is not None and sorted(config.channel_order) != sorted(channels):
            raise ValueError("link bus order must name the source channels")
        self.domain, self.config, self.channels, self.mode = domain, config, channels, mode
        self.pending: deque[_Pending] = deque()
        self._ready: list[LinkOutput] = []
        self.history = np.empty(0, bool)
        self.identity: tuple[str, str, int] | None = None
        self.origin_index: int | None = None
        self.next_index: int | None = None
        self.watermark: int | None = None
        self.closed = False

    @property
    def pending_count(self) -> int:
        return sum(len(row.output.words) for row in self.pending)

    def push(self, source: ConvertedSamples) -> None:
        """Submit input before closing its sampling time; keep state across chunks."""
        if self.closed:
            raise ValueError("link input has finished")
        if (
            source.domain != self.domain
            or source.channels != self.channels
            or source.bits != self.config.bits
        ):
            raise ValueError("source clock/channels/bits differ from the link configuration")
        if (
            source.codes.ndim != 2
            or source.codes.shape[1] != len(self.channels)
            or source.codes.dtype.kind not in "iu"
        ):
            raise ValueError("source codes must be an integer array with one column per channel")
        if (
            type(source.first_index) is not int
            or source.first_index < 0
            or type(source.epoch) is not int
            or source.epoch < 0
        ):
            raise ValueError("source index and epoch must be integers with index >= 0")
        identity = (source.run_id, source.branch_id, source.session_id)
        if self.identity is not None and identity != self.identity:
            raise ValueError("source run/branch/session identity cannot change in a link")
        if not len(source):
            return
        if self.next_index is not None and source.first_index != self.next_index:
            raise ValueError("link input indices must be contiguous")
        if self.watermark is not None and source.sample_time(0) <= self.watermark:
            raise ValueError("source input cannot rewrite a closed time watermark")
        if source.codes.min() < 0 or source.codes.max() >= 2**self.config.bits:
            raise ValueError("source codes do not fit the configured bits")

        # Use the same vectorized word and time transform as the batch path, before any mutation.
        output = transmit(source, replace(self.config, overflow_offset_cycles=0), self.mode)
        output.config = self.config
        saturated = ((source.codes == 0) | (source.codes == 2**self.config.bits - 1)).any(axis=1)
        shift = self.config.overflow_offset_cycles
        if shift >= 0:
            joined = np.concatenate((self.history, saturated))
            offset = len(self.history) - shift
            start = min(len(source), max(0, -offset))
            output.overflow = np.zeros(len(source), bool)
            output.overflow_valid = np.zeros(len(source), bool)
            output.overflow[start:] = joined[offset + start : offset + len(source)]
            output.overflow_valid[start:] = True
            new_history = joined[-min(shift, len(joined)) :].copy() if shift else np.empty(0, bool)
            pending = _Pending(output, np.ones(len(source), bool))
        else:
            output.overflow = np.zeros(len(source), bool)
            output.overflow_valid = np.zeros(len(source), bool)
            pending = _Pending(output, np.zeros(len(source), bool))
            new_history = self.history

        self.pending.append(pending)
        if shift < 0:
            # Indicator at j refers to saturation at j-shift; populate it when that source arrives.
            for row in self.pending:
                first = row.output.first_index
                lo = max(first, source.first_index + shift)
                hi = min(first + len(row.output.words), source.first_index + len(source) + shift)
                if hi > lo:
                    a, b = lo - first, hi - first
                    row.output.overflow[a:b] = saturated[
                        lo - shift - source.first_index : hi - shift - source.first_index
                    ]
                    row.output.overflow_valid[a:b] = True
                    row.known[a:b] = True
        self.history = new_history
        self.identity = identity
        if self.origin_index is None:
            self.origin_index = source.first_index
        self.next_index = source.first_index + len(source)

    def finish(self) -> None:
        """Declare finite input EOF; unavailable overflow references remain explicitly invalid."""
        for row in self.pending:
            row.output.overflow[~row.known] = False
            row.known[:] = True
        self.closed = True

    def advance(self, end_ps: int | None = None) -> None:
        if end_ps is None:
            if not self.closed:
                raise ValueError("open link input needs an explicit time watermark")
            end_ps = (
                int(self.pending[-1].output.capture_time[-1]) if self.pending else self.watermark
            )
            if end_ps is None:
                return
        if type(end_ps) is not int or self.watermark is not None and end_ps < self.watermark:
            raise ValueError("link time watermark must be an integer and cannot move backwards")
        # Reject unresolved lookahead atomically, before publishing even the earlier ready prefix.
        for row in self.pending:
            count = int(np.searchsorted(row.output.capture_time, end_ps, side="right"))
            if not row.known[:count].all():
                raise ValueError(
                    "overflow requires unseen future source samples; submit input or finish"
                )
        while self.pending:
            row = self.pending[0]
            count = int(np.searchsorted(row.output.capture_time, end_ps, side="right"))
            if count == 0:
                break
            self._ready.append(_slice(row.output, 0, count))
            self.pending.popleft()
            if count < len(row.output.words):
                self.pending.appendleft(
                    _Pending(
                        _slice(row.output, count, len(row.output.words)), row.known[count:].copy()
                    )
                )
                break
        self.watermark = end_ps

    def ready(self) -> list[LinkOutput]:
        return deepcopy(self._ready)

    def drain(self) -> list[LinkOutput]:
        ready, self._ready = self._ready, []
        return ready

    @staticmethod
    def _hash(value: dict) -> str:
        encoded = json.dumps(value, sort_keys=True, allow_nan=False).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _configuration(self) -> dict:
        return {
            "domain": asdict(self.domain),
            "config": asdict(self.config),
            "channels": self.channels,
            "mode": self.mode,
        }

    def checkpoint(self) -> bytes:
        payload = {
            "format": self.FORMAT,
            "config_hash": self._hash(self._configuration()),
            "state": {
                "origin_index": str(self.origin_index) if self.origin_index is not None else None,
                "next_index": str(self.next_index) if self.next_index is not None else None,
                "watermark": str(self.watermark) if self.watermark is not None else None,
                "identity": self.identity,
                "history": self.history.tolist(),
                "closed": self.closed,
                "pending": [
                    {"output": _encode(row.output), "known": row.known.tolist()}
                    for row in self.pending
                ],
                "ready": [_encode(output) for output in self._ready],
            },
        }
        payload["checksum"] = self._hash(payload)
        return json.dumps(payload, sort_keys=True, allow_nan=False).encode()

    @classmethod
    def restore(
        cls,
        domain: ClockDomain,
        config: LinkConfig,
        channels: tuple[int, ...],
        blob: bytes,
        mode: str = "research",
    ) -> StreamLink:
        payload = json.loads(blob)
        if not isinstance(payload, dict) or payload.get("format") != cls.FORMAT:
            raise ValueError("link checkpoint version is unsupported")
        restored = cls(domain, config, channels, mode)
        if payload.get("config_hash") != restored._hash(restored._configuration()):
            raise ValueError("link checkpoint configuration differs")
        checksum = payload.pop("checksum", None)
        if checksum != restored._hash(payload):
            raise ValueError("link checkpoint checksum is invalid")
        state = payload["state"]
        for name in ("origin_index", "next_index", "watermark"):
            setattr(restored, name, int(state[name]) if state[name] is not None else None)
        restored.identity = tuple(state["identity"]) if state["identity"] is not None else None
        restored.history = np.asarray(state["history"], bool)
        restored.closed = state["closed"]

        def decode(value: dict) -> LinkOutput:
            return LinkOutput(
                words=np.asarray(value["words"], np.uint16).reshape(-1, len(channels)),
                overflow=np.asarray(value["overflow"], bool),
                overflow_valid=np.asarray(value["overflow_valid"], bool),
                **{key: np.asarray([int(tick) for tick in value[key]], np.int64) for key in TIMES},
                first_index=int(value["first_index"]),
                epoch=value["epoch"],
                channels=config.channel_order or channels,
                domain=domain,
                config=config,
                run_id=value["run_id"],
                branch_id=value["branch_id"],
                session_id=value["session_id"],
            )

        restored.pending = deque(
            _Pending(decode(row["output"]), np.asarray(row["known"], bool))
            for row in state["pending"]
        )
        restored._ready = [decode(value) for value in state["ready"]]
        return restored
