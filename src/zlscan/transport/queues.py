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

import heapq
import json
from collections import deque
from dataclasses import asdict, dataclass, field
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


def simulate(
    items: list[Item], stages: list[StageSpec], end_ps: int | None = None
) -> TransportResult:
    for s in stages:
        if s.rate_Bps is None or s.capacity_bytes is None and s is stages[0]:
            raise ValueError(f"stage {s.name}: unknown rate or capacity; nothing is assumed")
        if s.rate_Bps <= 0:
            raise ValueError(f"stage {s.name}: rate must be positive")
    st = [_Stage(s) for s in stages]
    res = TransportResult(stats=[s.stats for s in st])
    heap: list[tuple[int, int, str, int, Item]] = []
    counter = 0

    def push(t: int, kind: str, i: int, item: Item) -> None:
        nonlocal counter
        heapq.heappush(heap, (t, counter, kind, i, item))
        counter += 1

    def lose(layer: str, reason: str, item: Item, t: int) -> None:
        res.losses.append(
            TransportLoss(layer, reason, item.epoch, item.clock_domain, item.first_sample_index,
                          item.first_sample_index + item.sample_count - 1, t, item.item_id)
        )  # fmt: skip

    def try_start(i: int, t: int) -> None:
        s = st[i]
        if s.serving is not None or s.blocked is not None or not s.queue:
            return
        item, enq = s.queue.popleft()
        s.serving = item
        s.stats.max_wait_ps = max(s.stats.max_wait_ps, t - enq)
        work = -(-item.bytes * PS_PER_S // int(s.spec.rate_Bps))
        push(finish_time(t, work, s.spec.stalls), "finish", i, item)

    def admit(i: int, item: Item, t: int) -> bool:
        s = st[i]
        if not s.room(item.bytes):
            return False
        s.queue.append((item, t))
        s.bytes += item.bytes
        s.note(t)
        try_start(i, t)
        return True

    def release(i: int, item: Item, t: int) -> None:
        s = st[i]
        s.serving = None
        s.blocked = None
        s.bytes -= item.bytes
        s.note(t)
        if i > 0 and st[i - 1].blocked is not None:
            advance(i - 1, t)
        try_start(i, t)

    def advance(i: int, t: int) -> None:
        """Stage i holds a finished item; try to hand it to stage i+1 or deliver it."""
        s = st[i]
        item = s.blocked or s.serving
        assert item is not None
        if i == len(st) - 1:
            if any(a <= t < a + d for a, d in s.spec.fail_windows):
                lose(s.spec.name, "write_failed", item, t)
            else:
                res.delivered.append((item, t))
            release(i, item, t)
            return
        nxt = st[i + 1]
        if nxt.room(item.bytes):
            s.serving = None
            s.blocked = None
            s.bytes -= item.bytes
            s.note(t)
            nxt.queue.append((item, t))
            nxt.bytes += item.bytes
            nxt.note(t)
            if i > 0 and st[i - 1].blocked is not None:
                advance(i - 1, t)
            try_start(i, t)
            try_start(i + 1, t)
        elif not nxt.spec.backpressure:
            nxt.stats.overflow_count += 1
            lose(nxt.spec.name, "overflow", item, t)
            release(i, item, t)
        else:
            s.blocked = item  # keep it; retried when the next stage frees space
            s.serving = None

    for it in items:
        push(it.arrival_ps, "arrive", 0, it)
    while heap:
        t, _, kind, i, item = heapq.heappop(heap)
        if end_ps is not None and t > end_ps:
            heapq.heappush(heap, (t, 0, kind, i, item))
            break
        if kind == "arrive":
            if not admit(0, item, t):
                st[0].stats.overflow_count += 1
                lose(st[0].spec.name, "overflow", item, t)
        else:
            st[i].serving = item
            advance(i, t)
    for s in st:
        for it, _ in s.queue:
            res.in_flight.append(it)
        for it in (s.serving, s.blocked):
            if it is not None:
                res.in_flight.append(it)
    seen_ids = {x.item_id for x in res.in_flight}
    for _, _, kind, _, it in heap:
        if kind == "arrive" and it.item_id not in seen_ids:
            res.in_flight.append(it)
            seen_ids.add(it.item_id)
    return res


def write_losses(path: Path, result: TransportResult) -> None:
    path.write_text(json.dumps([asdict(x) for x in result.losses], indent=1), encoding="utf-8")
