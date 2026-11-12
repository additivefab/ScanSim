"""ADC in-flight state, overflow lookahead and source identity across checkpoints."""

from dataclasses import replace

import numpy as np
import pytest

from zlscan.adc.patterns import ConvertedSamples
from zlscan.adc_link.link import LinkConfig, receive, transmit
from zlscan.contracts.time import ClockDomain

DOMAIN = ClockDomain(7, 125_000_003, phase_ticks=2**53 + 17)


def source(first=2**32 - 5, n=73, epoch=4):
    codes = np.column_stack((np.arange(n) + 100, np.arange(n) * 3 + 10)).astype(np.uint16)
    if n > 7:
        codes[[0, 6, n - 1], 0] = [0, 65535, 0]
    return ConvertedSamples(
        codes,
        (0, 1),
        first,
        epoch,
        DOMAIN,
        run_id="source-run",
        branch_id="source-branch",
        session_id=19,
    )


def part(src, first, end, epoch=None):
    return replace(
        src,
        codes=src.codes[first:end],
        first_index=src.first_index + first,
        epoch=src.epoch if epoch is None else epoch,
    )


def assert_stream_matches(outputs, expected):
    assert sum(len(out.words) for out in outputs) == len(expected.words)
    for field in (
        "words",
        "overflow",
        "overflow_valid",
        "sample_time",
        "adc_output_time",
        "pin_arrival_time",
        "capture_time",
    ):
        actual = np.concatenate([getattr(out, field) for out in outputs])
        np.testing.assert_array_equal(actual, getattr(expected, field))
    index = expected.first_index
    for out in outputs:
        assert out.first_index == index
        assert (out.run_id, out.branch_id, out.session_id) == ("source-run", "source-branch", 19)
        index += len(out.words)


@pytest.mark.parametrize("shift", [-5, -1, 0, 1, 5, 100])
@pytest.mark.parametrize("block_size", [1, 4, 19, 73])
@pytest.mark.parametrize("edge", ["rising", "falling"])
def test_stream_and_checkpoint_equal_complete_batch(shift, block_size, edge):
    from zlscan.adc_link.stream import StreamLink

    src = source()
    cfg = LinkConfig(
        coding="twos_complement",
        bit_map=tuple(reversed(range(16))),
        channel_order=(1, 0),
        capture_edge=edge,
        latency_half_cycles=25,
        propagation_ps=1500,
        overflow_offset_cycles=shift,
    )
    expected = transmit(src, cfg)
    engine = StreamLink(DOMAIN, cfg, src.channels)
    outputs = []
    for start in range(0, len(src), block_size):
        end = min(len(src), start + block_size)
        engine.push(part(src, start, end))
        engine.advance(src.sample_time(end - 1))
        saved = engine.checkpoint()
        assert saved == engine.checkpoint()
        engine = StreamLink.restore(DOMAIN, cfg, src.channels, saved)
        assert engine.checkpoint() == saved
        outputs.extend(engine.drain())
    engine.finish()
    engine.advance()
    outputs.extend(engine.drain())
    assert_stream_matches(outputs, expected)
    assert engine.pending_count == 0


def test_pipeline_preserves_old_epoch_until_its_actual_capture():
    from zlscan.adc_link.stream import StreamLink

    src = source(n=24)
    cfg = LinkConfig(latency_half_cycles=24, propagation_ps=100)
    engine = StreamLink(DOMAIN, cfg, src.channels)
    engine.push(part(src, 0, 8, epoch=4))
    engine.advance(src.sample_time(7))
    assert engine.drain() == [] and engine.pending_count == 8
    engine.push(part(src, 8, 24, epoch=5))
    engine = StreamLink.restore(DOMAIN, cfg, src.channels, engine.checkpoint())
    engine.finish()
    engine.advance()
    outputs = engine.drain()
    assert [(out.epoch, len(out.words)) for out in outputs] == [(4, 8), (5, 16)]
    assert_stream_matches(outputs, transmit(src, cfg))


def test_pending_outputs_survive_checkpoint_and_result_queries_do_not_mutate():
    from zlscan.adc_link.stream import StreamLink

    src = source(n=20)
    cfg = LinkConfig(latency_half_cycles=4, propagation_ps=0)
    engine = StreamLink(DOMAIN, cfg, src.channels)
    engine.push(src)
    engine.advance(src.sample_time(10))
    before = engine.checkpoint()
    ready = engine.ready()
    assert ready and engine.pending_count
    ready[0].words[:] = 0
    assert before == engine.checkpoint()
    restored = StreamLink.restore(DOMAIN, cfg, src.channels, before)
    first = restored.drain()
    restored.finish()
    restored.advance()
    assert_stream_matches(first + restored.drain(), transmit(src, cfg))


def test_unseen_overflow_future_cannot_be_published_and_eof_marks_it_invalid():
    from zlscan.adc_link.stream import StreamLink

    src = source(n=4)
    cfg = LinkConfig(latency_half_cycles=0, propagation_ps=0, overflow_offset_cycles=-2)
    engine = StreamLink(DOMAIN, cfg, src.channels)
    engine.push(part(src, 0, 1))
    before = engine.checkpoint()
    with pytest.raises(ValueError, match="overflow"):
        engine.advance(src.sample_time(0))
    assert engine.checkpoint() == before
    engine.push(part(src, 1, 4))
    engine.finish()
    engine.advance()
    outputs = engine.drain()
    assert_stream_matches(outputs, transmit(src, cfg))
    assert np.concatenate([out.overflow_valid for out in outputs]).tolist() == [
        True,
        True,
        False,
        False,
    ]


def test_receiver_preserves_identity_and_never_wraps_overflow_at_finite_boundaries():
    src = source(n=8)
    cfg = LinkConfig(latency_half_cycles=0, propagation_ps=0, overflow_offset_cycles=2)
    got = receive(transmit(src, cfg), cfg, rx_overflow_offset_cycles=2)
    assert got.sample_id(0, 1) == src.sample_id(0, 1)
    assert got.meta["overflow_valid"].tolist() == [True] * 6 + [False] * 2
    # The prefix overflow must not wrap into the missing tail.
    assert got.meta["overflow"][-2:].tolist() == [False, False]


def test_rejected_input_and_mismatched_checkpoints_leave_state_unchanged():
    import json

    from zlscan.adc_link.stream import StreamLink

    src = source(n=8)
    cfg = LinkConfig(latency_half_cycles=24, propagation_ps=100)
    engine = StreamLink(DOMAIN, cfg, src.channels)
    engine.push(part(src, 0, 4))
    engine.advance(src.sample_time(3))
    before = engine.checkpoint()
    for bad in (
        part(src, 0, 1),
        replace(part(src, 4, 8), session_id=20),
        replace(part(src, 4, 8), domain=ClockDomain(8, 50_000_000)),
    ):
        with pytest.raises(ValueError):
            engine.push(bad)
        assert engine.checkpoint() == before
    with pytest.raises(ValueError, match="configuration"):
        StreamLink.restore(DOMAIN, replace(cfg, propagation_ps=101), src.channels, before)
    broken = json.loads(before)
    broken["state"]["next_index"] = "999"
    with pytest.raises(ValueError, match="checksum"):
        StreamLink.restore(DOMAIN, cfg, src.channels, json.dumps(broken).encode())
    engine.finish()
    with pytest.raises(ValueError):
        engine.push(part(src, 4, 8))


def test_incremental_link_requires_explicit_delays():
    from zlscan.adc_link.stream import StreamLink

    with pytest.raises(ValueError, match="explicit"):
        StreamLink(DOMAIN, LinkConfig(), (0,))


@pytest.mark.parametrize(
    "shift, expected",
    [
        (2, [False, False, False, False, True, True, False, False]),
        (-2, [False, False, True, False, False, False, False, False]),
    ],
)
def test_receiver_shift_never_recycles_flags_from_other_end(shift, expected):
    src = source(n=8)
    cfg = LinkConfig(latency_half_cycles=0, propagation_ps=0)
    got = receive(transmit(src, cfg), cfg, rx_overflow_offset_cycles=shift)
    assert got.meta["overflow"].tolist() == expected
    assert got.meta["overflow_valid"].tolist() == (
        [True] * 6 + [False] * 2 if shift > 0 else [False] * 2 + [True] * 6
    )


def test_pipeline_time_overflow_is_rejected_instead_of_wrapping():
    src = replace(
        source(n=2), first_index=0, domain=ClockDomain(7, 1_000_000_000, phase_ticks=2**63 - 100)
    )
    cfg = LinkConfig(latency_half_cycles=24, propagation_ps=0)
    # The source array itself must be representable to test overflow in the link delay.
    src = replace(src, codes=src.codes[:1])
    with pytest.raises(ValueError, match="64-bit"):
        transmit(src, cfg)


def test_empty_input_and_repeated_finish_do_not_invent_samples():
    from zlscan.adc_link.stream import StreamLink

    src = source(n=0)
    cfg = LinkConfig(latency_half_cycles=24, propagation_ps=0)
    engine = StreamLink(DOMAIN, cfg, src.channels)
    before = engine.checkpoint()
    engine.push(src)
    assert engine.checkpoint() == before
    engine.finish()
    engine.finish()
    engine.advance()
    assert engine.drain() == [] and engine.pending_count == 0


@pytest.mark.parametrize("edge", ["rising", "falling"])
def test_rational_clock_times_match_independent_edge_search(edge):
    src = source(n=4)
    cfg = LinkConfig(
        latency_half_cycles=25, propagation_ps=1500, capture_edge=edge, dco_phase_ps=-499
    )
    out = transmit(src, cfg)
    half_clock = ClockDomain(DOMAIN.id, 2 * DOMAIN.freq_num, phase_ticks=DOMAIN.phase_ticks)
    edge_index = int(edge == "falling")
    for position in range(len(src)):
        index = src.first_index + position
        sample = DOMAIN.phase_ticks + index * 10**12 // DOMAIN.freq_num
        output_time = half_clock.tick_of(2 * index + 25)
        pin = output_time + 1500
        count = index + 10
        while half_clock.tick_of(2 * count + edge_index) - 499 < pin:
            count += 1
        assert int(out.sample_time[position]) == sample
        assert int(out.adc_output_time[position]) == output_time
        assert int(out.pin_arrival_time[position]) == pin
        assert int(out.capture_time[position]) == half_clock.tick_of(2 * count + edge_index) - 499


def test_long_stream_draining_keeps_only_pipeline_and_overflow_state():
    from zlscan.adc_link.stream import StreamLink

    cfg = LinkConfig(latency_half_cycles=25, propagation_ps=1500, overflow_offset_cycles=-5)
    engine = StreamLink(DOMAIN, cfg, (0, 1))
    count, sizes, max_pending = 0, [], 0
    first = 2**32 - 5
    for index in range(1500):
        src = source(first=first + index * 7, n=7)
        engine.push(src)
        engine.advance(src.sample_time(6))
        count += sum(len(out.words) for out in engine.drain())
        max_pending = max(max_pending, engine.pending_count)
        if index and index % 100 == 0:
            sizes.append(len(engine.checkpoint()))
            engine = StreamLink.restore(DOMAIN, cfg, (0, 1), engine.checkpoint())
    engine.finish()
    engine.advance()
    count += sum(len(out.words) for out in engine.drain())
    assert count == 10_500
    assert max_pending <= 14
    assert max(sizes) < 10_000 and max(sizes) - min(sizes) < 200
