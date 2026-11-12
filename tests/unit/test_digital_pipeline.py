"""ADC/capture/queue composition resumes with its actual pending code payloads."""

import json
from dataclasses import asdict

import numpy as np
import pytest

from zlscan.adc.patterns import ConvertedSamples
from zlscan.adc_link.link import LinkConfig
from zlscan.contracts.time import CaptureClock, ClockDomain
from zlscan.digital.acquisition import AcquisitionConfig, channel_mask
from zlscan.io.rawstream import FLAG_EPOCH_START, RawWriter, read_all
from zlscan.transport.queues import StageSpec

DOMAIN = ClockDomain(7, 125_000_003, phase_ticks=2**53 + 17)
ACQ = AcquisitionConfig(2, 7, sync_latency_cycles=2)


def run(edge, block_size, stalled, restore):
    from zlscan.engine.digital_pipeline import DigitalPipeline

    link = LinkConfig(
        latency_half_cycles=25,
        propagation_ps=1500,
        capture_edge=edge,
        coding="twos_complement",
        bit_map=tuple(reversed(range(16))),
        channel_order=(1, 0),
        overflow_offset_cycles=-5,
    )
    clock = CaptureClock(DOMAIN, edge=edge)
    first = 2**32 - 5
    stages = [
        StageSpec("rx", 184, 2e9),
        StageSpec(
            "disk",
            92,
            1e9 if stalled else 1e12,
            stalls=((clock.tick_of(first + 30), 500_000),) if stalled else (),
        ),
    ]
    engine = DigitalPipeline(DOMAIN, link, ACQ, stages, (0, 1))
    engine.schedule_syncs([clock.tick_of(first + offset) for offset in (12, 30, 48, 66)])
    codes = np.column_stack((100 + np.arange(73), 1000 + np.arange(73))).astype(np.uint16)
    boundaries = sorted({0, 25, 73, *range(0, 73, block_size)})
    packets, losses, ignored, skipped = [], [], [], []
    checkpoints = []
    for start, end in zip(boundaries, boundaries[1:], strict=False):
        source = ConvertedSamples(
            codes[start:end],
            (0, 1),
            first + start,
            4 if start < 25 else 5,
            DOMAIN,
            run_id="source",
            branch_id="branch",
            session_id=19,
        )
        engine.push(source)
        if restore:
            blob = engine.checkpoint()
            checkpoints.append(json.loads(blob))
            engine = DigitalPipeline.restore(DOMAIN, link, ACQ, stages, (0, 1), blob)
            assert engine.checkpoint() == blob
        result = engine.drain()
        packets.extend(result.delivered)
        losses.extend(result.losses)
        ignored.extend(result.ignored_syncs)
        skipped.extend(result.skipped_lines)
    engine.finish()
    result = engine.drain()
    packets.extend(result.delivered)
    losses.extend(result.losses)
    ignored.extend(result.ignored_syncs)
    skipped.extend(result.skipped_lines)
    return engine, packets, losses, ignored, skipped, checkpoints


def canonical(result):
    _, packets, losses, ignored, skipped, _ = result
    return (
        [
            {"packet": packet.packet.to_dict(), "delivery_tick": str(packet.delivery_tick)}
            for packet in packets
        ],
        [asdict(loss) for loss in losses],
        ignored,
        skipped,
    )


@pytest.mark.parametrize("edge", ["rising", "falling"])
@pytest.mark.parametrize("block_size", [1, 7, 17, 73])
@pytest.mark.parametrize("stalled", [False, True])
def test_composed_pipeline_resume_equals_continuous_bytes_and_loss_ledger(
    tmp_path, edge, block_size, stalled
):
    result = run(edge, block_size, stalled, True)
    expected = run(edge, 73, stalled, False)
    assert canonical(result) == canonical(expected)

    def write(name, packets):
        path = tmp_path / name
        previous = None
        with RawWriter(path, result[0].session_header(19)) as writer:
            for delivered in packets:
                packet = delivered.packet
                source = packet.source
                writer.write_block(
                    epoch=source.epoch,
                    channel_mask=channel_mask(source.channels),
                    clock_domain=source.domain.id,
                    first_sample_index=source.first_index,
                    samples=source.codes,
                    flags=packet.flags | (FLAG_EPOCH_START if source.epoch != previous else 0),
                    line_number=packet.item.line_number,
                )
                previous = source.epoch
        return path

    actual = write("actual.zlraw", result[1])
    baseline = write("expected.zlraw", expected[1])
    assert actual.read_bytes() == baseline.read_bytes()
    raw = read_all(actual)
    assert not raw.fatal and not raw.errors
    assert raw.session.windows["selection_basis"] == "capture_clock"
    assert raw.session.windows["capture_clock"]["edge"] == edge
    if not stalled:
        assert [
            (p.packet.source.first_index - (2**32 - 5), len(p.packet.source), p.packet.source.epoch)
            for p in result[1]
        ] == [(3, 7, 4), (21, 4, 4), (25, 3, 5), (39, 7, 5), (57, 7, 5)]
        assert not result[2]
    else:
        assert result[2]
    assert result[0].pending_packet_count == 0


def test_checkpoint_contains_adc_interface_window_and_queued_payloads():
    result = run("rising", 7, True, True)
    snapshots = result[-1]
    assert any(snapshot["state"]["pending_packets"] for snapshot in snapshots)
    assert any(snapshot["state"]["acquisition"]["state"]["active"] for snapshot in snapshots)
    assert any(snapshot["state"]["link"]["state"]["pending"] for snapshot in snapshots)


def test_zero_latency_eof_truncation_is_scheduled_after_closed_producer_time():
    from zlscan.engine.digital_pipeline import DigitalPipeline

    domain = ClockDomain(1, 1_000_000)
    link = LinkConfig(latency_half_cycles=0, propagation_ps=0)
    config = AcquisitionConfig(0, 10, sync_latency_cycles=0)
    stages = [StageSpec("disk", 1024, 1e12)]
    engine = DigitalPipeline(domain, link, config, stages, (0,))
    engine.schedule_syncs([0])
    for start in (0, 4):
        engine.push(
            ConvertedSamples(
                np.arange(start, start + 4, dtype=np.uint16).reshape(-1, 1), (0,), start, 0, domain
            )
        )
    engine.finish()
    saved = engine.checkpoint()
    restored = DigitalPipeline.restore(domain, link, config, stages, (0,), saved)
    assert restored.checkpoint() == saved
    result = restored.drain()
    assert len(result.delivered) == 1
    packet = result.delivered[0].packet
    assert (
        packet.window["missing_count"] == 2
        and packet.window["termination_reason"] == "end_of_input"
    )
    assert len(packet.source) == 8
    assert packet.item.arrival_ps == domain.tick_of(7) + 1


def test_long_pipeline_drained_state_is_bounded_by_active_work():
    from zlscan.engine.digital_pipeline import DigitalPipeline

    link = LinkConfig(latency_half_cycles=24, propagation_ps=0)
    config = AcquisitionConfig(0, 8, sync_latency_cycles=0)
    stages = [StageSpec("disk", 1024, 1e12)]
    engine = DigitalPipeline(DOMAIN, link, config, stages, (0, 1))
    first = 2**32 - 5
    clock = CaptureClock(DOMAIN)
    sizes = []
    count = 0
    for line in range(1000):
        start = first + line * 32
        engine.schedule_syncs([clock.tick_of(start + 12)])
        source = ConvertedSamples(np.full((32, 2), 100, np.uint16), (0, 1), start, 0, DOMAIN)
        engine.push(source)
        result = engine.drain()
        count += sum(len(p.packet.source) for p in result.delivered)
        assert not result.losses
        if line % 100 == 0:
            sizes.append(len(engine.checkpoint()))
    engine.finish()
    count += sum(len(p.packet.source) for p in engine.drain().delivered)
    assert count == 8000
    assert max(sizes) < 20000 and max(sizes) - min(sizes) < 512
    assert engine.pending_packet_count == 0


def test_invalid_source_identity_does_not_partly_advance_pipeline():
    from zlscan.engine.digital_pipeline import DigitalPipeline

    link = LinkConfig(latency_half_cycles=24, propagation_ps=0)
    engine = DigitalPipeline(DOMAIN, link, ACQ, [StageSpec("disk", 1024, 1e12)], (0, 1))
    saved = engine.checkpoint()
    source = ConvertedSamples(np.zeros((1, 2), np.uint16), (0, 1), 0, 0, DOMAIN, session_id=None)
    with pytest.raises(ValueError, match="identity"):
        engine.push(source)
    assert engine.checkpoint() == saved
