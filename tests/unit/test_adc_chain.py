"""ADC test codes, output link and the digital acquisition reference model (S2-T01..T03)."""

import numpy as np
import pytest

from zlscan.adc import patterns
from zlscan.adc_link.link import LinkConfig, LinkConfigError, receive, transmit
from zlscan.contracts.time import ClockDomain
from zlscan.digital.acquisition import (
    AcquisitionConfig,
    AcquisitionConfigError,
    acquire,
    validate,
    write_raw,
)
from zlscan.io.rawstream import read_all

DOM = ClockDomain(id=1, freq_num=50_000_000)  # 20 ns period


def source(n=64, per=None):
    per = per or {0: patterns.ramp(100, 3, n), 1: patterns.prbs(7, n)}
    return patterns.make_source(per, DOM)


# ---- S2-T01 ---------------------------------------------------------------------------------


def test_patterns_follow_their_definitions():
    assert patterns.fixed(0x1234, 5).tolist() == [0x1234] * 5
    assert patterns.alternating(1, 2, 5).tolist() == [1, 2, 1, 2, 1]
    assert patterns.ramp(65534, 1, 4).tolist() == [65534, 65535, 0, 1]
    assert patterns.ramp(10, 5, 3).tolist() == [10, 15, 20]


def test_prbs_is_reproducible_from_its_seed_and_differs_between_seeds():
    assert np.array_equal(patterns.prbs(3, 100), patterns.prbs(3, 100))
    assert not np.array_equal(patterns.prbs(3, 100), patterns.prbs(4, 100))


def test_codes_must_fit_the_bit_width():
    with pytest.raises(ValueError):
        patterns.fixed(70000, 3)
    with pytest.raises(ValueError):
        patterns.make_source({0: patterns.fixed(1, 3), 1: patterns.fixed(1, 4)}, DOM)


def test_codes_from_a_file(tmp_path):
    f = tmp_path / "codes.txt"
    f.write_text("1\n2\n3\n")
    assert patterns.from_file(f).tolist() == [1, 2, 3]
    f.write_text("1\n70000\n")
    with pytest.raises(ValueError):
        patterns.from_file(f)


def test_samples_know_their_identity_and_instant():
    s = patterns.make_source({0: patterns.ramp(0, 1, 10), 1: patterns.ramp(0, 2, 10)}, DOM,
                             first_index=100, epoch=3)  # fmt: skip
    sid = s.sample_id(4, 1)
    assert (sid.epoch, sid.channel, sid.sample_index, sid.clock_domain) == (3, 1, 104, 1)
    assert s.sample_time(4) == DOM.tick_of(104) == 104 * 20_000


# ---- S2-T02 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("coding", ["offset_binary", "twos_complement"])
def test_a_matching_receiver_recovers_the_source_codes(coding):
    src = source()
    cfg = LinkConfig(coding=coding, bit_map=tuple(reversed(range(16))), channel_order=(1, 0),
                     latency_half_cycles=24, propagation_ps=1500)  # fmt: skip
    out = transmit(src, cfg)
    assert out.channels == (1, 0)
    got = receive(out, cfg)
    assert np.array_equal(got.codes, src.codes)


def test_twos_complement_changes_the_pin_word_but_not_the_recovered_code():
    src = source(4, {0: patterns.fixed(0x0001, 4)})
    a = transmit(src, LinkConfig(coding="offset_binary"))
    b = transmit(src, LinkConfig(coding="twos_complement"))
    assert a.words[0, 0] == 0x0001 and b.words[0, 0] == 0x8001


@pytest.mark.parametrize(
    "rx",
    [
        LinkConfig(coding="twos_complement"),
        LinkConfig(bit_map=tuple(reversed(range(16)))),
        LinkConfig(channel_order=(1, 0)),
    ],
)
def test_a_receiver_that_disagrees_recovers_wrong_codes(rx):
    src = source()
    got = receive(transmit(src, LinkConfig()), rx)
    assert not np.array_equal(got.codes, src.codes)


def test_each_delay_is_counted_once_in_four_timestamps():
    pulse = patterns.make_source({0: patterns.alternating(0, 40000, 8)}, DOM)
    cfg = LinkConfig(latency_half_cycles=25, propagation_ps=1500, dco_phase_ps=0)
    out = transmit(pulse, cfg)
    i = 3
    assert out.adc_output_time[i] - out.sample_time[i] == 12.5 * 20_000  # pipeline only
    assert out.pin_arrival_time[i] - out.adc_output_time[i] == 1500  # propagation only
    wait = out.capture_time[i] - out.pin_arrival_time[i]
    assert 0 <= wait < 20_000  # waiting for the next capture edge, less than one period


def test_the_falling_edge_captures_half_a_period_later_on_the_grid():
    src = source(8)
    r = transmit(src, LinkConfig(latency_half_cycles=24, propagation_ps=0, capture_edge="rising"))
    f = transmit(src, LinkConfig(latency_half_cycles=24, propagation_ps=0, capture_edge="falling"))
    assert np.all((f.capture_time - r.capture_time) == 10_000)


def test_hardware_constrained_refuses_an_unread_configuration():
    with pytest.raises(LinkConfigError, match="register_readback"):
        transmit(
            source(), LinkConfig(latency_half_cycles=24, propagation_ps=0), "hardware_constrained"
        )
    ok = LinkConfig(latency_half_cycles=24, propagation_ps=0, register_readback=True)
    transmit(source(), ok, "hardware_constrained")


def test_a_bit_map_must_be_a_permutation():
    with pytest.raises(LinkConfigError):
        transmit(source(), LinkConfig(bit_map=(0,) * 16))


def test_overflow_alignment_follows_the_transmitter_offset():
    src = patterns.make_source({0: np.array([5, 0, 5, 5, 5, 5], dtype=np.uint16)}, DOM)
    out = transmit(src, LinkConfig(overflow_offset_cycles=2))
    assert out.overflow.tolist() == [False, False, False, True, False, False]
    aligned = receive(out, LinkConfig(), rx_overflow_offset_cycles=2)
    assert aligned.meta["overflow"].tolist()[1] is True


# ---- S2-T03 ---------------------------------------------------------------------------------

LINE_TICKS = 200 * 20_000  # one line every 200 ADC cycles
CFG = AcquisitionConfig(window_start=10, window_length=40)


def syncs(n, offset=7_000, jitter=None):
    jitter = jitter or [0] * n
    return [offset + i * LINE_TICKS + jitter[i] for i in range(n)]


def test_every_kept_line_has_exactly_the_window_length_and_tags_follow_the_data():
    src = patterns.make_source({0: patterns.ramp(0, 1, 1000)}, DOM)
    res = acquire(src, syncs(4), CFG)
    assert [len(x.samples) for x in res.lines] == [40] * 4
    for line in res.lines:
        # the ramp value equals the sample index, so data and tag must agree
        assert line.samples[0, 0] == line.start_index
        assert line.samples[-1, 0] == line.start_index + 39


def test_the_sampling_phase_follows_the_phase_of_the_line_sync():
    src = patterns.make_source({0: patterns.ramp(0, 1, 2000)}, DOM)
    res = acquire(src, syncs(3, jitter=[0, 5_000, 12_000]), CFG)
    phases = [x.phase_ps for x in res.lines]
    assert len(set(phases)) == 3 and all(0 <= p < 20_000 for p in phases)


def test_a_sync_inside_an_open_line_is_ignored_and_counted():
    src = patterns.make_source({0: patterns.ramp(0, 1, 1000)}, DOM)
    extra = syncs(2) + [syncs(1)[0] + 30 * 20_000]
    res = acquire(src, extra, CFG)
    assert len(res.lines) == 2 and len(res.ignored_syncs) == 1


def test_a_missing_sync_leaves_a_gap_in_line_starts():
    src = patterns.make_source({0: patterns.ramp(0, 1, 1000)}, DOM)
    s = syncs(4)
    res = acquire(src, [s[0], s[1], s[3]], CFG)
    starts = [x.start_index for x in res.lines]
    assert starts[2] - starts[1] == 2 * 200


def test_the_synchroniser_latency_shifts_the_window():
    src = patterns.make_source({0: patterns.ramp(0, 1, 1000)}, DOM)
    a = acquire(src, syncs(1), AcquisitionConfig(10, 40, sync_latency_cycles=2))
    b = acquire(src, syncs(1), AcquisitionConfig(10, 40, sync_latency_cycles=3))
    assert b.lines[0].start_index - a.lines[0].start_index == 1


def test_a_window_cut_by_the_end_of_data_is_marked_truncated():
    src = patterns.make_source({0: patterns.ramp(0, 1, 250)}, DOM)
    res = acquire(src, [230 * 20_000], CFG)
    assert res.lines[0].truncated and len(res.lines[0].samples) < 40


def test_raw_codes_cannot_be_averaged_on_a_log_front_end():
    with pytest.raises(AcquisitionConfigError, match="raw codes"):
        validate(
            AcquisitionConfig(0, 4, aggregation="mean", value_domain="raw_code", front_end="log")
        )
    with pytest.raises(AcquisitionConfigError, match="lookup"):
        validate(AcquisitionConfig(0, 4, aggregation="mean", value_domain="linear"))


def test_written_blocks_read_back_with_their_line_numbers(tmp_path):
    src = patterns.make_source({0: patterns.ramp(0, 1, 1000), 1: patterns.prbs(1, 1000)}, DOM)
    res = acquire(src, syncs(3), CFG)
    write_raw(tmp_path / "a.zlraw", src, res, CFG)
    back = read_all(tmp_path / "a.zlraw")
    assert [b.line_number for b in back.blocks] == [0, 1, 2]
    assert not back.errors and back.losses == []
