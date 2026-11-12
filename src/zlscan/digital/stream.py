"""Incremental digital windows; source identities remain distinct from capture-edge counts."""

from __future__ import annotations

import hashlib
import heapq
import json
from copy import deepcopy
from dataclasses import asdict, dataclass, field

import numpy as np

from zlscan.adc.patterns import ConvertedSamples
from zlscan.contracts.time import CaptureClock, ClockDomain
from zlscan.digital.acquisition import AcquisitionConfig, _first_edge_at_or_after, validate


@dataclass
class CapturedWindow:
    line_number: int
    sync_tick: int
    counter_zero_index: int
    capture_start_index: int
    window_length: int
    phase_ps: int
    channels: tuple[int, ...]
    segments: list[ConvertedSamples] = field(default_factory=list)
    termination_reason: str | None = None
    outside_initial: bool = False
    clock_basis: str = "source_grid"
    completion_tick: int | None = None

    @property
    def capture_end_index(self) -> int:
        return self.capture_start_index + self.window_length

    @property
    def start_index(self) -> int | None:
        return self.segments[0].first_index if self.segments else None

    @property
    def samples(self) -> np.ndarray:
        return (
            np.concatenate([segment.codes for segment in self.segments])
            if self.segments
            else np.empty((0, len(self.channels)), np.uint16)
        )

    @property
    def missing_count(self) -> int:
        return self.window_length - sum(len(segment) for segment in self.segments)

    @property
    def truncated(self) -> bool:
        return self.missing_count != 0

    @property
    def epoch(self) -> int | None:
        epochs = {segment.epoch for segment in self.segments}
        return next(iter(epochs)) if len(epochs) == 1 else None


@dataclass
class AcquisitionBatch:
    windows: list[CapturedWindow] = field(default_factory=list)
    ignored_syncs: list[int] = field(default_factory=list)
    skipped_lines: list[tuple[int, str]] = field(default_factory=list)


class StreamAcquisition:
    FORMAT = "zlscan-acquisition-checkpoint/1"

    def __init__(
        self,
        domain: ClockDomain,
        config: AcquisitionConfig,
        channels: tuple[int, ...],
        *,
        capture_clock: CaptureClock | None = None,
        first_line_number: int = 0,
    ) -> None:
        validate(config)
        if config.aggregation is not None:
            raise NotImplementedError("board-side aggregation is not implemented")
        if type(first_line_number) is not int or first_line_number < 0:
            raise ValueError("first line number must be a nonnegative integer")
        if not channels or len(set(channels)) != len(channels):
            raise ValueError("source channels must be nonempty and unique")
        self.domain, self.config, self.channels = domain, config, channels
        self.capture_clock = capture_clock
        self.clock = capture_clock or domain
        self.events: list[tuple[int, int, int]] = []  # counter zero, physical tick, sequence
        self.sequence = 0
        self.busy_until = 0
        self.next_line_number = first_line_number
        self.active: CapturedWindow | None = None
        self.watermark: int | None = None
        self.first_capture_count: int | None = None
        self.last_capture_count: int | None = None
        self.next_source_index: int | None = None
        self.identity: tuple[str, str, int] | None = None
        self.bits: int | None = None
        self.closed = False
        self._result = AcquisitionBatch()

    def schedule_syncs(self, ticks: list[int]) -> None:
        if self.closed:
            raise ValueError("acquisition input has finished")
        if any(
            type(tick) is not int or self.watermark is not None and tick <= self.watermark
            for tick in ticks
        ):
            raise ValueError("sync tick must be an integer after the closed time watermark")
        for tick in ticks:
            zero = _first_edge_at_or_after(self.clock, tick) + self.config.sync_latency_cycles
            heapq.heappush(self.events, (zero, tick, self.sequence))
            self.sequence += 1

    def _append(
        self, source: ConvertedSamples | None, counts: np.ndarray | None, times: np.ndarray | None
    ) -> None:
        window = self.active
        if window is None or source is None or window.outside_initial:
            return
        lo = int(np.searchsorted(counts, window.capture_start_index))
        hi = int(np.searchsorted(counts, window.capture_end_index))
        if hi <= lo:
            return
        meta = {"selection_time": times[lo:hi].copy(), "window_count": counts[lo:hi].copy()}
        if self.capture_clock is not None:
            meta.update(capture_time=times[lo:hi].copy(), capture_count=counts[lo:hi].copy())
        elif "capture_time" in source.meta:
            meta["capture_time"] = np.asarray(source.meta["capture_time"])[lo:hi].copy()
        for key in ("overflow", "overflow_valid"):
            if key in source.meta:
                meta[key] = np.asarray(source.meta[key], bool)[lo:hi].copy()
        window.segments.append(
            ConvertedSamples(
                source.codes[lo:hi].astype(np.uint16, copy=True),
                self.channels,
                source.first_index + lo,
                source.epoch,
                source.domain,
                source.bits,
                source.run_id,
                source.branch_id,
                source.session_id,
                meta,
            )
        )

    def _complete(self) -> None:
        window = self.active
        assert window is not None
        if window.outside_initial or not window.segments:
            self._result.skipped_lines.append(
                (window.line_number, "window outside the captured samples")
            )
        else:
            window.termination_reason = (
                "complete"
                if not window.truncated
                else "end_of_input"
                if self.closed
                else "missing_input"
            )
            window.completion_tick = (
                self.watermark
                if self.closed and window.truncated
                else self.clock.tick_of(window.capture_end_index - 1)
            )
            self._result.windows.append(window)
        self.active = None

    def _run(
        self,
        until: int,
        source: ConvertedSamples | None = None,
        counts: np.ndarray | None = None,
        times: np.ndarray | None = None,
    ) -> None:
        while self.events and self.events[0][0] <= until:
            zero, tick, _ = heapq.heappop(self.events)
            if self.active is not None and self.active.capture_end_index <= zero:
                self._append(source, counts, times)
                self._complete()
            if zero < self.busy_until:
                self._result.ignored_syncs.append(tick)
                continue
            start = zero + self.config.window_start
            self.active = CapturedWindow(
                self.next_line_number,
                tick,
                zero,
                start,
                self.config.window_length,
                self.clock.tick_of(zero - self.config.sync_latency_cycles) - tick,
                self.channels,
                outside_initial=self.first_capture_count is not None
                and start < self.first_capture_count,
                clock_basis="capture_clock" if self.capture_clock is not None else "source_grid",
            )
            self.next_line_number += 1
            self.busy_until = start + self.config.window_length
        if self.active is not None:
            self._append(source, counts, times)
            if self.active.capture_end_index - 1 <= until:
                self._complete()

    def push(self, source: ConvertedSamples) -> None:
        if self.closed:
            raise ValueError("acquisition input has finished")
        if source.domain != self.domain or source.channels != self.channels:
            raise ValueError("source clock or channels differ from acquisition configuration")
        if (
            source.codes.ndim != 2
            or source.codes.shape[1] != len(self.channels)
            or source.codes.dtype.kind not in "iu"
            or type(source.bits) is not int
            or not 1 <= source.bits <= 16
        ):
            raise ValueError("acquisition needs a declared integer ADC code array")
        if (
            type(source.first_index) is not int
            or source.first_index < 0
            or type(source.epoch) is not int
            or source.epoch < 0
        ):
            raise ValueError("source index and epoch must be nonnegative integers")
        identity = (source.run_id, source.branch_id, source.session_id)
        if (
            self.identity is not None
            and identity != self.identity
            or self.bits is not None
            and self.bits != source.bits
        ):
            raise ValueError("source run/branch/session/bits cannot change in an acquisition")
        if not len(source):
            return
        if self.next_source_index is not None and source.first_index != self.next_source_index:
            raise ValueError("source indices must be contiguous; gaps need explicit loss handling")
        if source.codes.min() < 0 or source.codes.max() >= 2**source.bits:
            raise ValueError("source codes do not fit their declared bits")
        if self.capture_clock is not None:
            if "capture_time" not in source.meta:
                raise ValueError("capture clock mode requires actual capture_time records")
            times = np.asarray(source.meta["capture_time"])
            if times.shape != (len(source),) or times.dtype.kind not in "iu":
                raise ValueError("capture_time must be an integer vector matching the code rows")
            values = [self.clock.count_at(int(tick)) for tick in times]
            counts = np.asarray(
                values,
                dtype=object if any(abs(value) > 2**63 - 1 for value in values) else np.int64,
            )
            if any(
                self.clock.tick_of(int(count)) != int(tick)
                for count, tick in zip(counts, times, strict=True)
            ):
                raise ValueError("input capture_time does not lie on the declared capture clock")
        else:
            # Explicit legacy/source-grid policy: metadata capture times do not change selection.
            times = source.sample_times()
            if source.first_index + len(source) - 1 > 2**63 - 1:
                counts = np.fromiter(
                    (source.first_index + i for i in range(len(source))),
                    dtype=object,
                    count=len(source),
                )
            else:
                counts = np.arange(
                    source.first_index, source.first_index + len(source), dtype=np.int64
                )
        if (
            np.any(times[1:] <= times[:-1])
            or np.any(counts[1:] <= counts[:-1])
            or self.watermark is not None
            and int(times[0]) <= self.watermark
        ):
            raise ValueError("capture inputs cannot rewrite closed time or repeat edges")
        for key in ("overflow", "overflow_valid", "capture_time"):
            if key in source.meta and np.asarray(source.meta[key]).shape != (len(source),):
                raise ValueError(f"{key} must match the code rows")
        if (
            "capture_time" in source.meta
            and np.asarray(source.meta["capture_time"]).dtype.kind not in "iu"
        ):
            raise ValueError("capture_time must be an integer vector")

        for key in ("overflow", "overflow_valid"):
            if key in source.meta:
                flags = np.asarray(source.meta[key])
                if flags.dtype.kind not in "biu" or np.any((flags != 0) & (flags != 1)):
                    raise ValueError(f"{key} must contain boolean flags")
        if (
            not isinstance(source.run_id, str)
            or not isinstance(source.branch_id, str)
            or type(source.session_id) is not int
            or source.session_id < 0
        ):
            raise ValueError(
                "source identity needs run/branch strings and a nonnegative session integer"
            )

        self.identity, self.bits = identity, source.bits
        if self.first_capture_count is None:
            self.first_capture_count = int(counts[0])
            if (
                self.active is not None
                and self.active.capture_start_index < self.first_capture_count
            ):
                self.active.outside_initial = True
        self._run(int(counts[-1]), source, counts, times)
        self.last_capture_count = int(counts[-1])
        self.next_source_index = source.first_index + len(source)
        self.watermark = int(times[-1])

    def advance(self, tick: int) -> None:
        if (
            self.closed
            or type(tick) is not int
            or self.watermark is not None
            and tick < self.watermark
        ):
            raise ValueError("acquisition watermark must be monotonic and input open")
        self._run(self.clock.count_at(tick))
        self.watermark = tick

    def finish(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.events:
            self._run(max(event[0] for event in self.events))
        if self.active is not None:
            self._complete()

    def result(self) -> AcquisitionBatch:
        return deepcopy(self._result)

    def drain(self) -> AcquisitionBatch:
        result, self._result = self._result, AcquisitionBatch()
        return result

    def _configuration(self) -> dict:
        return {
            "domain": asdict(self.domain),
            "config": asdict(self.config),
            "channels": self.channels,
            "capture_clock": asdict(self.capture_clock) if self.capture_clock else None,
        }

    @staticmethod
    def _hash(value: dict) -> str:
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, allow_nan=False).encode()
        ).hexdigest()

    @staticmethod
    def _encode_window(window: CapturedWindow) -> dict:
        value = {
            key: getattr(window, key)
            for key in (
                "line_number",
                "sync_tick",
                "counter_zero_index",
                "capture_start_index",
                "window_length",
                "phase_ps",
                "termination_reason",
                "outside_initial",
                "clock_basis",
                "completion_tick",
            )
        }
        for key in (
            "line_number",
            "sync_tick",
            "counter_zero_index",
            "capture_start_index",
            "phase_ps",
        ):
            value[key] = str(value[key])
        value["completion_tick"] = (
            str(window.completion_tick) if window.completion_tick is not None else None
        )
        value["segments"] = [
            {
                "codes": segment.codes.tolist(),
                "first_index": str(segment.first_index),
                "epoch": segment.epoch,
                "bits": segment.bits,
                "run_id": segment.run_id,
                "branch_id": segment.branch_id,
                "session_id": segment.session_id,
                "meta": {
                    key: (
                        [str(int(v)) for v in array]
                        if key
                        in {"capture_time", "capture_count", "selection_time", "window_count"}
                        else array.tolist()
                    )
                    for key, array in segment.meta.items()
                },
            }
            for segment in window.segments
        ]
        return value

    def checkpoint(self) -> bytes:
        payload = {
            "format": self.FORMAT,
            "config_hash": self._hash(self._configuration()),
            "state": {
                **{
                    name: str(getattr(self, name)) if getattr(self, name) is not None else None
                    for name in (
                        "sequence",
                        "busy_until",
                        "next_line_number",
                        "watermark",
                        "first_capture_count",
                        "last_capture_count",
                        "next_source_index",
                    )
                },
                "identity": self.identity,
                "bits": self.bits,
                "closed": self.closed,
                "events": [[str(zero), str(tick), str(seq)] for zero, tick, seq in self.events],
                "active": self._encode_window(self.active) if self.active else None,
                "windows": [self._encode_window(window) for window in self._result.windows],
                "ignored_syncs": [str(tick) for tick in self._result.ignored_syncs],
                "skipped_lines": [
                    [str(line), reason] for line, reason in self._result.skipped_lines
                ],
            },
        }
        payload["checksum"] = self._hash(payload)
        return json.dumps(payload, sort_keys=True, allow_nan=False).encode()

    @classmethod
    def restore(
        cls,
        domain: ClockDomain,
        config: AcquisitionConfig,
        channels: tuple[int, ...],
        blob: bytes,
        *,
        capture_clock: CaptureClock | None = None,
    ) -> StreamAcquisition:
        payload = json.loads(blob)
        if not isinstance(payload, dict) or payload.get("format") != cls.FORMAT:
            raise ValueError("acquisition checkpoint version is unsupported")
        restored = cls(domain, config, channels, capture_clock=capture_clock)
        if payload.get("config_hash") != cls._hash(restored._configuration()):
            raise ValueError("acquisition checkpoint configuration differs")
        if payload.pop("checksum", None) != cls._hash(payload):
            raise ValueError("acquisition checkpoint checksum is invalid")
        state = payload["state"]
        for name in (
            "sequence",
            "busy_until",
            "next_line_number",
            "watermark",
            "first_capture_count",
            "last_capture_count",
            "next_source_index",
        ):
            setattr(restored, name, int(state[name]) if state[name] is not None else None)
        restored.identity = tuple(state["identity"]) if state["identity"] is not None else None
        restored.bits, restored.closed = state["bits"], state["closed"]
        restored.events = [tuple(int(value) for value in row) for row in state["events"]]

        def decode(value: dict) -> CapturedWindow:
            fields = {
                key: int(value[key])
                for key in (
                    "line_number",
                    "sync_tick",
                    "counter_zero_index",
                    "capture_start_index",
                    "phase_ps",
                )
            }
            segments = [
                ConvertedSamples(
                    np.asarray(segment["codes"], np.uint16).reshape(-1, len(channels)),
                    channels,
                    int(segment["first_index"]),
                    segment["epoch"],
                    domain,
                    segment["bits"],
                    segment["run_id"],
                    segment["branch_id"],
                    segment["session_id"],
                    {
                        key: np.asarray(
                            [int(v) for v in array],
                            object if key in {"capture_count", "window_count"} else np.int64,
                        )
                        if key
                        in {"capture_time", "capture_count", "selection_time", "window_count"}
                        else np.asarray(array, bool)
                        for key, array in segment["meta"].items()
                    },
                )
                for segment in value["segments"]
            ]
            return CapturedWindow(
                **fields,
                channels=channels,
                segments=segments,
                window_length=value["window_length"],
                termination_reason=value["termination_reason"],
                outside_initial=value["outside_initial"],
                clock_basis=value["clock_basis"],
                completion_tick=int(value["completion_tick"])
                if value["completion_tick"] is not None
                else None,
            )

        restored.active = decode(state["active"]) if state["active"] else None
        restored._result = AcquisitionBatch(
            [decode(window) for window in state["windows"]],
            [int(tick) for tick in state["ignored_syncs"]],
            [(int(line), reason) for line, reason in state["skipped_lines"]],
        )
        return restored
