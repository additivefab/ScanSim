"""Freeze pre-refactor results and resume genuinely busy event queues, including ties."""

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from zlscan.transport.queues import Item, StageSpec, simulate

PS = 1_000_000


def scenario(mode):
    arrivals = [0, 0, 4, 8, 12, 12, 20, 30, 100]
    items = [
        Item(i, 4, i // 4, 1, 2**53 + 17 + 4 * i, 4, at * PS, i) for i, at in enumerate(arrivals)
    ]
    stages = [
        StageSpec("rx", 24, 4e6),
        StageSpec(
            "disk",
            8,
            1e6 if mode else 8e6,
            stalls=((5 * PS, 50 * PS),) if mode else (),
            backpressure=mode != 2,
            fail_windows=((60 * PS, 20 * PS),) if mode == 3 else (),
        ),
    ]
    return items, stages


def digest(result):
    payload = asdict(result)
    # In-flight is an unordered conservation ledger, not a delivery sequence.
    payload["in_flight"].sort(key=lambda item: item["item_id"])
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@pytest.mark.parametrize("mode", range(4))
@pytest.mark.parametrize("cut", [None, 0, 15, 80, 1000])
def test_batch_results_match_frozen_pre_checkpoint_implementation(mode, cut):
    golden = json.loads(
        (Path(__file__).parent / "fixtures/transport_before_checkpoint.json").read_text()
    )
    items, stages = scenario(mode)
    result = simulate(items, stages, None if cut is None else cut * PS)
    assert digest(result) == golden[f"{mode}:{cut}"]
    assert result.conserved(items)


@pytest.mark.parametrize("mode", range(4))
@pytest.mark.parametrize("cut", [0, 2, 5, 15, 54, 55, 70, 100, 1000])
def test_checkpoint_and_repeated_queries_preserve_complete_result(mode, cut):
    from zlscan.transport.queues import TransportSimulation

    items, stages = scenario(mode)
    engine = TransportSimulation(stages, items)
    engine.advance(cut * PS)
    before = engine.checkpoint()
    assert engine.checkpoint() == before
    saved_result = engine.result()
    assert engine.result() == saved_result
    restored = TransportSimulation.restore(stages, before)
    assert restored.result() == saved_result
    assert restored.checkpoint() == before
    restored.advance()
    engine.advance()
    assert restored.result() == engine.result() == simulate(items, stages)


def test_checkpoint_contains_serving_blocked_queue_and_future_events():
    from zlscan.transport.queues import TransportSimulation

    items, stages = scenario(1)
    engine = TransportSimulation(stages, items)
    engine.advance(15 * PS)
    state = json.loads(engine.checkpoint())["state"]
    assert any(row["blocked"] is not None for row in state["stages"])
    assert any(row["serving"] is not None for row in state["stages"])
    assert any(row["queue"] for row in state["stages"])
    assert {row[2] for row in state["events"]} == {"arrive", "finish"}


def test_incremental_input_and_drained_history_match_one_batch():
    from zlscan.transport.queues import TransportSimulation

    items, stages = scenario(3)
    expected = simulate(items, stages)
    engine = TransportSimulation(stages)
    deliveries, losses = [], []
    occupancy = [[] for _ in stages]
    for index, item in enumerate(items):
        engine.schedule([item])
        # All arrivals at a given instant are submitted before closing that instant.
        if index + 1 < len(items) and items[index + 1].arrival_ps == item.arrival_ps:
            continue
        engine.advance(item.arrival_ps)
        delta = engine.drain()
        deliveries.extend(delta.delivered)
        losses.extend(delta.losses)
        for target, stats in zip(occupancy, delta.stats, strict=True):
            target.extend(stats.occupancy)
        engine = TransportSimulation.restore(stages, engine.checkpoint())
    engine.advance()
    delta = engine.drain()
    deliveries.extend(delta.delivered)
    losses.extend(delta.losses)
    assert deliveries == expected.delivered
    assert losses == expected.losses
    for target, stats, final in zip(occupancy, delta.stats, expected.stats, strict=True):
        assert target + stats.occupancy == final.occupancy
        assert stats.peak_bytes == final.peak_bytes
    assert engine.result().delivered == []
    assert engine.result().losses == []


def test_restore_rejects_config_changes_and_damaged_or_old_checkpoints():
    from zlscan.transport.queues import TransportSimulation

    items, stages = scenario(1)
    engine = TransportSimulation(stages, items)
    blob = engine.checkpoint()
    with pytest.raises(ValueError, match="configuration"):
        TransportSimulation.restore([StageSpec("rx", 1, 4e6)], blob)
    broken = json.loads(blob)
    broken["state"]["counter"] += 1
    with pytest.raises(ValueError, match="checksum"):
        TransportSimulation.restore(stages, json.dumps(broken).encode())
    broken["format"] = "future-version"
    with pytest.raises(ValueError, match="version"):
        TransportSimulation.restore(stages, json.dumps(broken).encode())


def test_inputs_cannot_rewrite_closed_time_or_reuse_item_ids():
    from zlscan.transport.queues import TransportSimulation

    engine = TransportSimulation([StageSpec("rx", 100, 1e9)])
    item = Item(4, 4, 0, 1, 0, 2, 10)
    engine.schedule([item])
    engine.advance(10)
    before = engine.checkpoint()
    for bad in [Item(5, 4, 0, 1, 2, 2, 10), Item(4, 4, 0, 1, 2, 2, 11)]:
        with pytest.raises(ValueError):
            engine.schedule([bad])
        assert engine.checkpoint() == before
    with pytest.raises(ValueError):
        engine.advance(9)


def test_fractional_service_rate_is_not_truncated():
    item = Item(0, 1, 0, 1, 0, 1, 0)
    assert simulate([item], [StageSpec("slow", 1, 0.5)]).delivered == [(item, 2 * 10**12)]
    assert simulate([item], [StageSpec("slow", 1, 1.5)]).delivered == [(item, 666_666_666_667)]


def test_long_incremental_stream_retains_backlog_instead_of_entire_history():
    from zlscan.transport.queues import TransportSimulation

    stages = [StageSpec("rx", 8, 4e6)]
    engine = TransportSimulation(stages)
    sizes, delivered = [], 0
    for index in range(2000):
        arrival = index * 10 * PS
        engine.schedule([Item(index, 4, 0, 1, index * 2, 2, arrival)])
        engine.advance(arrival + 5 * PS)
        delta = engine.drain()
        delivered += len(delta.delivered)
        assert not delta.losses and not delta.in_flight
        if index % 125 == 0:
            sizes.append(len(engine.checkpoint()))
    assert delivered == 2000
    assert max(sizes) < 2000 and max(sizes) - min(sizes) < 128
    assert engine.result().stats[0].peak_bytes == 4


def test_result_and_drained_snapshots_cannot_mutate_live_queues():
    from zlscan.transport.queues import TransportSimulation

    items, stages = scenario(1)
    engine = TransportSimulation(stages, items)
    engine.advance(15 * PS)
    before = engine.checkpoint()
    result = engine.result()
    result.stats[0].occupancy.clear()
    result.delivered.clear()
    result.in_flight.clear()
    assert engine.checkpoint() == before
