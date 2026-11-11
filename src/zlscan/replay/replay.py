"""Checkpoints and replay (S3-T14): resume from the nearest checkpoint, play a window forward.

A replayed window is published separately and never written back into the original run. The
claim is "the discrete model was restored exactly and run forward", which is what ``REPLAYED``
means; it is not a claim of physical exactness.
"""

from __future__ import annotations

import bisect
import time
from dataclasses import dataclass, field

from zlscan.contracts.query import QueryStatus
from zlscan.engine.chain import Chain, ChainConfig, ChunkOut


@dataclass
class CheckpointStore:
    interval_steps: int
    blobs: dict[int, bytes] = field(default_factory=dict)

    def nearest(self, step: int) -> int:
        keys = sorted(self.blobs)
        i = bisect.bisect_right(keys, step) - 1
        if i < 0:
            raise LookupError("no checkpoint at or before the requested step")
        return keys[i]

    @property
    def total_bytes(self) -> int:
        return sum(len(b) for b in self.blobs.values())


def run_with_checkpoints(cfg: ChainConfig, steps: int, interval_steps: int,
                         chunk: int | None = None) -> tuple[ChunkOut, CheckpointStore]:  # fmt: skip
    """Run and save a checkpoint at step 0 and every ``interval_steps`` steps."""
    chain = Chain(cfg)
    store = CheckpointStore(interval_steps)
    store.blobs[0] = chain.checkpoint()
    chunk = chunk or interval_steps
    parts, done = [], 0
    while done < steps:
        m = min(chunk, steps - done, interval_steps - done % interval_steps)
        parts.append(chain.advance(m))
        done += m
        if done % interval_steps == 0:
            store.blobs[done] = chain.checkpoint()
    from zlscan.engine.chain import merge

    return merge(parts), store


@dataclass
class ReplayResult:
    status: QueryStatus
    checkpoint_step: int
    start_step: int
    out: ChunkOut
    wall_s: float
    discarded_steps: int


def replay_window(cfg: ChainConfig, store: CheckpointStore, start_step: int, n_steps: int,
                  probes: set[str] | None = None) -> ReplayResult:  # fmt: skip
    t0 = time.perf_counter()
    base = store.nearest(start_step)
    chain = Chain(cfg)
    chain.restore(store.blobs[base])
    lead = start_step - base
    if lead:
        chain.advance(lead)  # warm-up to the window start; not published
    chain.probe_names = set(probes or ())
    out = chain.advance(n_steps)
    return ReplayResult(QueryStatus.REPLAYED, base, start_step, out, time.perf_counter() - t0, lead)
