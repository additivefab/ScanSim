"""Stateful windows, pending syncs and source/capture counter separation."""

from dataclasses import replace

import numpy as np
import pytest

from zlscan.adc.patterns import ConvertedSamples
from zlscan.adc_link.link import LinkConfig, receive, transmit
from zlscan.contracts.time import ClockDomain
from zlscan.digital.acquisition import AcquisitionConfig, acquire

DOMAIN = ClockDomain(7, 125_000_003, phase_ticks=2**53 + 17)
CONFIG = AcquisitionConfig(2, 7, sync_latency_cycles=2)


def source(n=73):
    codes = np.column_stack((100 + np.arange(n), 1000 + np.arange(n))).astype(np.uint16)
    return ConvertedSamples(
        codes, (0, 1), 2**32 - 5, 4, DOMAIN, run_id="source", branch_id="branch", session_id=19
    )


def part(src, start, end, epoch=None):
    meta = {key: value[start:end] for key, value in src.meta.items()}
    return replace(
        src,
        codes=src.codes[start:end],
        first_index=src.first_index + start,
        epoch=src.epoch if epoch is None else epoch,
        meta=meta,
    )


def syncs(src):
    return [
        DOMAIN.tick_of(src.first_index + offset) + 1
        for offset in (-6, 0, 3, 17, 17, 34, 52, 65, 100)
    ]


def collect(engine, windows, ignored, skipped):
    result = engine.drain()
    windows.extend(result.windows)
    ignored.extend(result.ignored_syncs)
    skipped.extend(result.skipped_lines)


@pytest.mark.parametrize("block_size", [1, 3, 11, 73])
def test_source_grid_matches_batch_acquire_through_checkpoints(block_size):
    from zlscan.digital.stream import StreamAcquisition

    src = source()
    ticks = syncs(src)
    expected = acquire(src, ticks, CONFIG, first_line_number=7)
    engine = StreamAcquisition(DOMAIN, CONFIG, src.channels, first_line_number=7)
    engine.schedule_syncs(ticks)
    windows, ignored, skipped = [], [], []
    for start in range(0, len(src), block_size):
        engine.push(part(src, start, min(len(src), start + block_size)))
        saved = engine.checkpoint()
        assert saved == engine.checkpoint()
        engine = StreamAcquisition.restore(DOMAIN, CONFIG, src.channels, saved)
        assert engine.checkpoint() == saved
        collect(engine, windows, ignored, skipped)
    engine.finish()
    collect(engine, windows, ignored, skipped)
    assert ignored == expected.ignored_syncs and skipped == expected.skipped_lines
    assert len(windows) == len(expected.lines)
    for got, want in zip(windows, expected.lines, strict=True):
        assert (
            got.line_number,
            got.sync_tick,
            got.start_index,
            got.counter_zero_index,
            got.phase_ps,
            got.truncated,
        ) == (
            want.line_number,
            want.sync_tick,
            want.start_index,
            want.counter_zero_index,
            want.phase_ps,
            want.truncated,
        )
        np.testing.assert_array_equal(got.samples, want.samples)
        assert all(
            segment.run_id == "source" and segment.session_id == 19 for segment in got.segments
        )


def test_cross_epoch_window_retains_each_source_segment():
    from zlscan.digital.stream import StreamAcquisition

    src = source()
    engine = StreamAcquisition(DOMAIN, CONFIG, src.channels)
    engine.schedule_syncs(syncs(src))
    engine.push(part(src, 0, 25, 4))
    engine = StreamAcquisition.restore(DOMAIN, CONFIG, src.channels, engine.checkpoint())
    engine.push(part(src, 25, len(src), 5))
    engine.finish()
    window = next(w for w in engine.drain().windows if w.start_index == src.first_index + 22)
    assert [(s.epoch, len(s)) for s in window.segments] == [(4, 3), (5, 4)]
    assert window.epoch is None and window.missing_count == 0


def test_partial_window_and_future_syncs_are_actually_in_checkpoint():
    import json

    from zlscan.digital.stream import StreamAcquisition

    src = source()
    engine = StreamAcquisition(DOMAIN, CONFIG, src.channels)
    engine.schedule_syncs(syncs(src))
    engine.push(part(src, 0, 25))
    state = json.loads(engine.checkpoint())["state"]
    assert state["active"] and state["active"]["segments"]
    assert state["events"] and state["next_line_number"]
    saved = engine.checkpoint()
    ready = engine.result()
    ready.windows[0].segments[0].codes[:] = 0
    ready.windows.clear()
    assert saved == engine.checkpoint()


@pytest.mark.parametrize("edge", ["rising", "falling"])
def test_capture_grid_window_keeps_source_identity_instead_of_capture_counter(edge):
    from zlscan.contracts.time import CaptureClock
    from zlscan.digital.stream import StreamAcquisition

    src = source()
    link = LinkConfig(latency_half_cycles=25, propagation_ps=1500, capture_edge=edge)
    got = receive(transmit(src, link), link)
    clock = CaptureClock(DOMAIN, edge=edge, phase_ticks=link.dco_phase_ps)
    count = clock.count_at(int(got.meta["capture_time"][8]))
    config = AcquisitionConfig(0, 4, sync_latency_cycles=0)
    engine = StreamAcquisition(DOMAIN, config, src.channels, capture_clock=clock)
    engine.schedule_syncs([clock.tick_of(count)])
    engine.push(got)
    engine.finish()
    window = engine.drain().windows[0]
    assert window.capture_start_index == count
    assert window.start_index == src.first_index + 8
    assert window.start_index != window.capture_start_index
    np.testing.assert_array_equal(window.samples, src.codes[8:12])
    assert window.segments[0].sample_id(0) == src.sample_id(8)


def test_rejected_late_sync_and_wrong_source_are_atomic():
    import json

    from zlscan.digital.stream import StreamAcquisition

    src = source()
    engine = StreamAcquisition(DOMAIN, CONFIG, src.channels)
    engine.schedule_syncs(syncs(src))
    engine.push(part(src, 0, 25))
    saved = engine.checkpoint()
    with pytest.raises(ValueError):
        engine.schedule_syncs([src.sample_time(24)])
    with pytest.raises(ValueError):
        engine.push(part(src, 0, 1))
    with pytest.raises(ValueError):
        engine.push(replace(part(src, 25, 30), session_id=20))
    assert engine.checkpoint() == saved
    with pytest.raises(ValueError, match="configuration"):
        StreamAcquisition.restore(DOMAIN, replace(CONFIG, window_length=8), src.channels, saved)
    broken = json.loads(saved)
    broken["state"]["next_line_number"] = "999"
    with pytest.raises(ValueError, match="checksum"):
        StreamAcquisition.restore(DOMAIN, CONFIG, src.channels, json.dumps(broken).encode())


def test_incremental_sync_input_does_not_need_whole_frame_plan():
    from zlscan.digital.stream import StreamAcquisition

    src = source()
    config = AcquisitionConfig(0, 4, sync_latency_cycles=0)
    engine = StreamAcquisition(DOMAIN, config, src.channels)
    windows, ignored, skipped = [], [], []
    for start in range(0, 72, 8):
        engine.schedule_syncs([src.sample_time(start)])
        engine.push(part(src, start, start + 8))
        collect(engine, windows, ignored, skipped)
    engine.finish()
    collect(engine, windows, ignored, skipped)
    assert [w.start_index for w in windows] == [src.first_index + i for i in range(0, 72, 8)]
    assert not ignored and not skipped


def test_source_grid_does_not_relabel_selection_time_as_observed_capture():
    from zlscan.digital.stream import StreamAcquisition

    src = source()
    engine = StreamAcquisition(DOMAIN, CONFIG, src.channels)
    engine.schedule_syncs(syncs(src))
    engine.push(src)
    engine.finish()
    window = engine.drain().windows[0]
    assert window.clock_basis == "source_grid"
    assert "capture_time" not in window.segments[0].meta
    assert "selection_time" in window.segments[0].meta


@pytest.mark.parametrize("edge", ["rising", "falling"])
@pytest.mark.parametrize("count", [-2, 0, 1, 2**32 + 3])
def test_capture_clock_inverse_preserves_its_exact_edge(edge, count):
    from zlscan.contracts.time import CaptureClock

    clock = CaptureClock(DOMAIN, edge=edge, phase_ticks=-499)
    tick = clock.tick_of(count)
    assert clock.count_at(tick) == count
    assert clock.first_at(tick) == count
    assert clock.count_at(tick - 1) == count - 1
    assert clock.first_at(tick + 1) == count + 1


@pytest.mark.parametrize("key", ["window_start", "window_length", "sync_latency_cycles"])
def test_fractional_window_counters_are_refused(key):
    from zlscan.digital.stream import StreamAcquisition

    with pytest.raises(ValueError, match="integers"):
        StreamAcquisition(DOMAIN, replace(CONFIG, **{key: 1.5}), (0, 1))


def test_source_counter_above_signed64_survives_window_checkpoint():
    from zlscan.digital.stream import StreamAcquisition

    first = 2**64 - 10
    domain = ClockDomain(7, 10**12, phase_ticks=2**53 + 17 - first)
    src = replace(source(8), domain=domain, first_index=first)
    config = AcquisitionConfig(0, 5, sync_latency_cycles=0)
    engine = StreamAcquisition(domain, config, src.channels)
    engine.schedule_syncs([src.sample_time(2)])
    engine.push(part(src, 0, 4))
    engine = StreamAcquisition.restore(domain, config, src.channels, engine.checkpoint())
    engine.push(part(src, 4, 8))
    engine.finish()
    window = engine.drain().windows[0]
    assert window.start_index == first + 2
    assert [
        int(count) for segment in window.segments for count in segment.meta["window_count"]
    ] == list(range(first + 2, first + 7))
    np.testing.assert_array_equal(window.samples, src.codes[2:7])


@pytest.mark.parametrize("value", [2, 0.5])
def test_invalid_observable_flags_are_rejected_without_state_change(value):
    from zlscan.digital.stream import StreamAcquisition

    src = source(8)
    engine = StreamAcquisition(DOMAIN, CONFIG, src.channels)
    saved = engine.checkpoint()
    with pytest.raises(ValueError, match="boolean"):
        engine.push(replace(src, meta={"overflow": np.full(8, value)}))
    assert engine.checkpoint() == saved
