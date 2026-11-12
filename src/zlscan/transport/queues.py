"""Queue graph between the ADC and the disk (S2-T04): a discrete-event simulation.

Items are the blocks written by the acquisition model. Every stage has a byte capacity and a
service rate, optionally with stall windows (a broken link is a stall that never ends). The
first stage cannot push back (the ADC cannot be stopped): an item arriving at a full first stage
is lost. A later stage pushes back by default: the stage before it keeps the finished item and
stops. A stage with ``backpressure=False`` drops instead. Nothing is overwritten silently: every
loss names the layer, the epoch and the exact range of source samples.

Time is integer picoseconds. Service that finds nothing to do is lost, never banked.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import math
from collections import deque
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path

PS_PER_S = 10**12
FOREVER = 10**18


@dataclass(frozen=True)
class StageSpec:
    name: str
    capacity_bytes: int | None
    rate_Bps: float | None
    stalls: tuple[tuple[int, int], ...] = ()  # (start_ps, duration_ps)
    backpressure: bool = True  # may the stage before it wait?
    fail_windows: tuple[tuple[int, int], ...] = ()  # items finishing here are lost (last stage)


@dataclass(frozen=True)
class Item:
    item_id: int
    bytes: int
    epoch: int
    clock_domain: int
    first_sample_index: int
    sample_count: int
    arrival_ps: int
    line_number: int = 0


@dataclass(frozen=True)
class TransportLoss:
    layer: str
    reason: str
    epoch: int
    clock_domain: int
    first_sample_index: int
    last_sample_index: int
    time_ps: int
    item_id: int


@dataclass
class StageStats:
    name: str
    peak_bytes: int = 0
    max_wait_ps: int = 0
    overflow_count: int = 0
    occupancy: list[tuple[int, int]] = field(default_factory=list)  # (time_ps, bytes)


@dataclass
class TransportResult:
    delivered: list[tuple[Item, int]] = field(default_factory=list)  # (item, delivery_ps)
    losses: list[TransportLoss] = field(default_factory=list)
    stats: list[StageStats] = field(default_factory=list)
    in_flight: list[Item] = field(default_factory=list)  # still inside when the run ended

    def conserved(self, items: list[Item]) -> bool:
        """Every item is delivered, lost or in flight, exactly once."""
        seen: dict[int, int] = {}
        for it, _ in self.delivered:
            seen[it.item_id] = seen.get(it.item_id, 0) + 1
        for loss in self.losses:
            seen[loss.item_id] = seen.get(loss.item_id, 0) + 1
        for it in self.in_flight:
            seen[it.item_id] = seen.get(it.item_id, 0) + 1
        return all(seen.get(it.item_id, 0) == 1 for it in items) and len(seen) == len(items)


def finish_time(start: int, work_ps: int, stalls: tuple[tuple[int, int], ...]) -> int:
    """When ``work_ps`` of service finishes if it starts at ``start`` and pauses during stalls."""
    t, remaining = start, work_ps
    for s, d in sorted(stalls):
        end = s + d
        if t >= end:
            continue
        if t < s:
            avail = s - t
            if remaining <= avail:
                return t + remaining
            remaining -= avail
        t = end
    return t + remaining


class _Stage:
    def __init__(self, spec: StageSpec) -> None:
        self.spec = spec
        self.queue: deque[tuple[Item, int]] = deque()  # (item, enqueue time)
        self.serving: Item | None = None
        self.blocked: Item | None = None
        self.bytes = 0
        self.stats = StageStats(spec.name)

    def room(self, n: int) -> bool:
        cap = self.spec.capacity_bytes
        return cap is None or self.bytes + n <= cap

    def note(self, t: int) -> None:
        self.stats.occupancy.append((t, self.bytes))
        self.stats.peak_bytes = max(self.stats.peak_bytes, self.bytes)


class TransportSimulation:
    """Incremental discrete-event transport with a complete, versioned queue checkpoint.

    A watermark closes all events at or before that instant. New batches must arrive later;
    their item IDs must exceed previously scheduled IDs. Drain emitted history for long streams.
    This is the transport component, not an atomic ADC/acquisition/disk checkpoint.
    """

    FORMAT = "zlscan-transport-checkpoint/1"

    def __init__(self, stages: list[StageSpec], items: list[Item] | None = None) -> None:
        if not stages or len({s.name for s in stages}) != len(stages):
            raise ValueError("transport needs at least one stage with unique names")
        for i, spec in enumerate(stages):
            if spec.rate_Bps is None or spec.capacity_bytes is None and i == 0:
                raise ValueError(f"stage {spec.name}: unknown rate or capacity; nothing is assumed")
            if (
                isinstance(spec.rate_Bps, bool)
                or not math.isfinite(spec.rate_Bps)
                or spec.rate_Bps <= 0
            ):
                raise ValueError(f"stage {spec.name}: rate must be positive and finite")
            if spec.capacity_bytes is not None and (
                type(spec.capacity_bytes) is not int or spec.capacity_bytes < 0
            ):
                raise ValueError("stage capacity must be a nonnegative integer or unknown")
            for start, duration in (*spec.stalls, *spec.fail_windows):
                if type(start) is not int or type(duration) is not int or duration < 0:
                    raise ValueError(
                        "stall and failure windows require integer time and duration >= 0"
                    )
        self.stages = [_Stage(deepcopy(spec)) for spec in stages]
        self._result = TransportResult(stats=[s.stats for s in self.stages])
        self.events: list[tuple[int, int, str, int, Item]] = []
        self.counter = 0
        self.watermark: int | None = None
        self.max_item_id: int | None = None
        self.schedule(items or [])

    def schedule(self, items: list[Item]) -> None:
        """Atomically add a batch before closing its arrival instants."""
        ids = [item.item_id for item in items]
        if any(type(value) is not int for value in ids) or len(set(ids)) != len(ids):
            raise ValueError("item IDs must be unique integers")
        if self.max_item_id is not None and any(value <= self.max_item_id for value in ids):
            raise ValueError("incremental item IDs must exceed previously scheduled IDs")
        for item in items:
            if any(type(getattr(item, key)) is not int for key in Item.__dataclass_fields__):
                raise ValueError("transport item fields must be integers")
            if item.bytes <= 0 or item.sample_count <= 0 or item.first_sample_index < 0:
                raise ValueError("item bytes/count must be positive and sample index nonnegative")
            if self.watermark is not None and item.arrival_ps <= self.watermark:
                raise ValueError("arrival cannot rewrite a closed transport time")
        for item in items:
            self._push(item.arrival_ps, "arrive", 0, item)
        if ids:
            self.max_item_id = max(ids)

    def _push(self, tick: int, kind: str, index: int, item: Item) -> None:
        heapq.heappush(self.events, (tick, self.counter, kind, index, item))
        self.counter += 1

    def _lose(self, layer: str, reason: str, item: Item, tick: int) -> None:
        self._result.losses.append(
            TransportLoss(layer, reason, item.epoch, item.clock_domain, item.first_sample_index,
                          item.first_sample_index + item.sample_count - 1, tick, item.item_id)
        )  # fmt: skip

    def _try_start(self, index: int, tick: int) -> None:
        stage = self.stages[index]
        if stage.serving is not None or stage.blocked is not None or not stage.queue:
            return
        item, enqueued = stage.queue.popleft()
        stage.serving = item
        stage.stats.max_wait_ps = max(stage.stats.max_wait_ps, tick - enqueued)
        rate = Fraction(str(stage.spec.rate_Bps))
        work = -(-item.bytes * PS_PER_S * rate.denominator // rate.numerator)
        self._push(finish_time(tick, work, stage.spec.stalls), "finish", index, item)

    def _admit(self, index: int, item: Item, tick: int) -> bool:
        stage = self.stages[index]
        if not stage.room(item.bytes):
            return False
        stage.queue.append((item, tick))
        stage.bytes += item.bytes
        stage.note(tick)
        self._try_start(index, tick)
        return True

    def _release(self, index: int, item: Item, tick: int) -> None:
        stage = self.stages[index]
        stage.serving = None
        stage.blocked = None
        stage.bytes -= item.bytes
        stage.note(tick)
        if index > 0 and self.stages[index - 1].blocked is not None:
            self._advance_stage(index - 1, tick)
        self._try_start(index, tick)

    def _advance_stage(self, index: int, tick: int) -> None:
        stage = self.stages[index]
        item = stage.blocked or stage.serving
        assert item is not None
        if index == len(self.stages) - 1:
            if any(start <= tick < start + duration for start, duration in stage.spec.fail_windows):
                self._lose(stage.spec.name, "write_failed", item, tick)
            else:
                self._result.delivered.append((item, tick))
            self._release(index, item, tick)
            return
        downstream = self.stages[index + 1]
        if downstream.room(item.bytes):
            stage.serving = None
            stage.blocked = None
            stage.bytes -= item.bytes
            stage.note(tick)
            downstream.queue.append((item, tick))
            downstream.bytes += item.bytes
            downstream.note(tick)
            if index > 0 and self.stages[index - 1].blocked is not None:
                self._advance_stage(index - 1, tick)
            self._try_start(index, tick)
            self._try_start(index + 1, tick)
        elif not downstream.spec.backpressure:
            downstream.stats.overflow_count += 1
            self._lose(downstream.spec.name, "overflow", item, tick)
            self._release(index, item, tick)
        else:
            stage.blocked = item
            stage.serving = None

    def advance(self, end_ps: int | None = None) -> None:
        """Process through the inclusive watermark, or drain all currently scheduled events."""
        if end_ps is not None and (
            type(end_ps) is not int or self.watermark is not None and end_ps < self.watermark
        ):
            raise ValueError("transport watermark must be an integer and cannot move backwards")
        while self.events and (end_ps is None or self.events[0][0] <= end_ps):
            tick, _, kind, index, item = heapq.heappop(self.events)
            self.watermark = tick
            if kind == "arrive":
                if not self._admit(0, item, tick):
                    self.stages[0].stats.overflow_count += 1
                    self._lose(self.stages[0].spec.name, "overflow", item, tick)
            else:
                self.stages[index].serving = item
                self._advance_stage(index, tick)
        if end_ps is not None:
            self.watermark = end_ps

    def result(self) -> TransportResult:
        """Independent read-only result snapshot, including future arrivals as in-flight."""
        result = deepcopy(self._result)
        for stage in self.stages:
            result.in_flight.extend(item for item, _ in stage.queue)
            result.in_flight.extend(
                item for item in (stage.serving, stage.blocked) if item is not None
            )
        seen = {item.item_id for item in result.in_flight}
        for _, _, kind, _, item in self.events:
            if kind == "arrive" and item.item_id not in seen:
                result.in_flight.append(item)
                seen.add(item.item_id)
        return result

    def drain(self) -> TransportResult:
        """Take emitted records/occupancy; retain current queues and cumulative extrema/counters."""
        result = self.result()
        self._result.delivered.clear()
        self._result.losses.clear()
        for stage in self.stages:
            stage.stats.occupancy.clear()
        return result

    @staticmethod
    def _hash(value: dict | list) -> str:
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, allow_nan=False).encode()
        ).hexdigest()

    def checkpoint(self) -> bytes:
        state = {
            "counter": self.counter,
            "watermark": self.watermark,
            "max_item_id": self.max_item_id,
            "events": [
                [tick, order, kind, index, asdict(item)]
                for tick, order, kind, index, item in self.events
            ],
            "stages": [
                {
                    "queue": [[asdict(item), tick] for item, tick in stage.queue],
                    "serving": asdict(stage.serving) if stage.serving is not None else None,
                    "blocked": asdict(stage.blocked) if stage.blocked is not None else None,
                    "bytes": stage.bytes,
                    "stats": asdict(stage.stats),
                }
                for stage in self.stages
            ],
            "delivered": [[asdict(item), tick] for item, tick in self._result.delivered],
            "losses": [asdict(loss) for loss in self._result.losses],
        }
        payload = {
            "format": self.FORMAT,
            "config_hash": self._hash([asdict(stage.spec) for stage in self.stages]),
            "state": state,
        }
        payload["checksum"] = self._hash(payload)
        return json.dumps(payload, sort_keys=True, allow_nan=False).encode()

    @classmethod
    def restore(cls, stages: list[StageSpec], blob: bytes) -> TransportSimulation:
        payload = json.loads(blob)
        if not isinstance(payload, dict) or payload.get("format") != cls.FORMAT:
            raise ValueError("transport checkpoint version is unsupported")
        if payload.get("config_hash") != cls._hash([asdict(spec) for spec in stages]):
            raise ValueError("transport checkpoint configuration differs")
        checksum = payload.pop("checksum", None)
        if checksum != cls._hash(payload):
            raise ValueError("transport checkpoint checksum is invalid")
        restored = cls(stages)
        state = payload["state"]
        restored.counter, restored.watermark = state["counter"], state["watermark"]
        restored.max_item_id = state["max_item_id"]
        restored.events = [
            (tick, order, kind, index, Item(**item))
            for tick, order, kind, index, item in state["events"]
        ]
        for stage, saved in zip(restored.stages, state["stages"], strict=True):
            stage.queue = deque((Item(**item), tick) for item, tick in saved["queue"])
            stage.serving = Item(**saved["serving"]) if saved["serving"] is not None else None
            stage.blocked = Item(**saved["blocked"]) if saved["blocked"] is not None else None
            stage.bytes = saved["bytes"]
            stats = saved["stats"]
            stage.stats = StageStats(
                **{**stats, "occupancy": [tuple(row) for row in stats["occupancy"]]}
            )
        restored._result = TransportResult(
            delivered=[(Item(**item), tick) for item, tick in state["delivered"]],
            losses=[TransportLoss(**row) for row in state["losses"]],
            stats=[stage.stats for stage in restored.stages],
        )
        return restored


def simulate(
    items: list[Item], stages: list[StageSpec], end_ps: int | None = None
) -> TransportResult:
    engine = TransportSimulation(stages, items)
    engine.advance(end_ps)
    return engine.result()


def write_losses(path: Path, result: TransportResult) -> None:
    path.write_text(json.dumps([asdict(x) for x in result.losses], indent=1), encoding="utf-8")
