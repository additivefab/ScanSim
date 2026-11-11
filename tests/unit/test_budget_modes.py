"""Acquisition modes, per-stage peak backlog, data contracts and board memory (S1-T04, S1-T05)."""

import pytest

from zlscan.budget.memory import FrameSpec, board_memory
from zlscan.budget.modes import (
    AcquisitionMode,
    Stage,
    assess,
    output_rate,
    simulate_stage_backlogs,
)
from zlscan.contracts.verdict import Verdict

MB = 1e6
DDR = 512 * 1024**2


def ddr_stage(capacity=40 * MB, service=100 * MB, stalls=()):
    return Stage("ddr", capacity_bytes=capacity, service_Bps=service, stalls=stalls)


# ---- continuous -----------------------------------------------------------------------------


def test_continuous_dual_125_msps_against_a_gigabit_link_fails():
    r = assess(AcquisitionMode.CONTINUOUS, input_Bps=500 * MB, stages=[ddr_stage(service=125 * MB)])
    assert r.verdict is Verdict.FAIL and "average" in r.reason


def test_continuous_below_the_service_rate_with_no_stalls_passes_the_average():
    r = assess(AcquisitionMode.CONTINUOUS, input_Bps=50 * MB, stages=[ddr_stage()])
    assert r.verdict is Verdict.PASS


def test_continuous_equal_rates_have_no_margin_and_fail():
    r = assess(AcquisitionMode.CONTINUOUS, input_Bps=100 * MB, stages=[ddr_stage()])
    assert r.verdict is Verdict.FAIL


def test_continuous_stalls_cost_average_service_and_peak_backlog():
    stalls = tuple((i * 0.010, 0.002) for i in range(200))  # 2 ms every 10 ms = 20 % stalled
    stage = ddr_stage(capacity=64_000, service=100 * MB, stalls=stalls)
    r = assess(AcquisitionMode.CONTINUOUS, input_Bps=50 * MB, stages=[stage], duration_s=2.0)
    assert r.stages[0].peak_backlog_bytes == pytest.approx(50 * MB * 0.002, rel=0.02)
    assert r.verdict is Verdict.FAIL, (
        "a 2 ms stall at 50 MB/s needs 100 kB of buffer, only 64 kB exist"
    )


# ---- finite frame ---------------------------------------------------------------------------


def test_finite_frame_may_run_faster_than_the_exit_if_the_buffer_holds_the_surplus():
    r = assess(AcquisitionMode.FINITE_FRAME, input_Bps=200 * MB, stages=[ddr_stage()],
               duration_s=0.1, drain_limit_s=0.5)  # fmt: skip
    assert r.verdict is Verdict.PASS
    assert r.stages[0].peak_backlog_bytes == pytest.approx(10 * MB, rel=1e-3)
    assert r.drain_time_s == pytest.approx(0.1, rel=0.02)


def test_the_same_input_is_rejected_continuously():
    cont = assess(AcquisitionMode.CONTINUOUS, input_Bps=200 * MB, stages=[ddr_stage()])
    assert cont.verdict is Verdict.FAIL


def test_finite_frame_fails_when_the_buffer_is_too_small():
    r = assess(AcquisitionMode.FINITE_FRAME, input_Bps=200 * MB,
               stages=[ddr_stage(capacity=5 * MB)],
               duration_s=0.1, drain_limit_s=0.5)  # fmt: skip
    assert r.verdict is Verdict.FAIL and r.stages[0].overflow


def test_finite_frame_fails_when_it_cannot_drain_in_time():
    r = assess(AcquisitionMode.FINITE_FRAME, input_Bps=200 * MB, stages=[ddr_stage()],
               duration_s=0.1, drain_limit_s=0.05)  # fmt: skip
    assert r.verdict is Verdict.FAIL and "drain" in r.reason


def test_finite_frame_with_an_unknown_service_rate_is_unresolved():
    r = assess(AcquisitionMode.FINITE_FRAME, input_Bps=200 * MB,
               stages=[Stage("ddr", capacity_bytes=40 * MB, service_Bps=None)],
               duration_s=0.1, drain_limit_s=0.5)  # fmt: skip
    assert r.verdict is Verdict.UNRESOLVED and "ddr" in r.reason


def test_finite_frame_with_an_unknown_capacity_is_unresolved():
    r = assess(AcquisitionMode.FINITE_FRAME, input_Bps=200 * MB,
               stages=[Stage("ddr", capacity_bytes=None, service_Bps=100 * MB)],
               duration_s=0.1, drain_limit_s=0.5)  # fmt: skip
    assert r.verdict is Verdict.UNRESOLVED


def test_a_cascade_piles_up_in_the_slowest_stage():
    fast = Stage("pl_fifo", capacity_bytes=1 * MB, service_Bps=400 * MB)
    slow = Stage("net", capacity_bytes=50 * MB, service_Bps=100 * MB)
    r = assess(AcquisitionMode.FINITE_FRAME, input_Bps=200 * MB, stages=[fast, slow],
               duration_s=0.1, drain_limit_s=0.5)  # fmt: skip
    by = {s.name: s for s in r.stages}
    assert by["pl_fifo"].peak_backlog_bytes < 0.1 * MB
    assert by["net"].peak_backlog_bytes == pytest.approx(10 * MB, rel=1e-2)


def test_idle_service_before_the_burst_is_not_banked():
    """Service that finds nothing to do is lost; a late burst sees the same backlog."""
    stage = ddr_stage()
    early = simulate_stage_backlogs(200 * MB, 0.1, [stage], start_s=0.0, horizon_s=1.0)
    late = simulate_stage_backlogs(200 * MB, 0.1, [stage], start_s=0.5, horizon_s=1.0)
    assert late[0].peak_backlog_bytes == pytest.approx(early[0].peak_backlog_bytes, rel=1e-3)
    assert late[0].peak_backlog_bytes == pytest.approx(10 * MB, rel=1e-3)


# ---- periodic burst -------------------------------------------------------------------------


def test_periodic_burst_passes_when_the_backlog_drains_before_the_next_burst():
    r = assess(AcquisitionMode.PERIODIC_BURST, input_Bps=200 * MB, stages=[ddr_stage()],
               duration_s=0.1, period_s=1.0)  # fmt: skip
    assert r.verdict is Verdict.PASS


def test_periodic_burst_fails_when_the_next_burst_arrives_on_a_non_empty_buffer():
    r = assess(AcquisitionMode.PERIODIC_BURST, input_Bps=200 * MB, stages=[ddr_stage()],
               duration_s=0.1, period_s=0.15)  # fmt: skip
    assert r.verdict is Verdict.FAIL and "next" in r.reason


# ---- data contracts -------------------------------------------------------------------------


def test_output_rate_counts_items_width_channels_flags_and_packet_overhead():
    assert output_rate(10e6, item_bytes=2, channels=1) == pytest.approx(20e6)
    assert output_rate(
        10e6, item_bytes=2, channels=2, flag_bytes=1, overhead_fraction=0.05
    ) == pytest.approx(10e6 * (2 * 2 + 1) * 1.05)


def test_decimation_changes_the_item_rate_not_just_the_byte_rate():
    raw = output_rate(50e6, item_bytes=2)
    five_to_one = output_rate(50e6 / 5, item_bytes=4, flag_bytes=1)  # wider items plus flags
    assert five_to_one > raw / 5, (
        "dividing the raw byte rate by the decimation ratio would understate it"
    )


def test_aggregating_raw_log_codes_by_averaging_is_refused():
    with pytest.raises(ValueError, match="log"):
        output_rate(
            10e6, item_bytes=2, aggregation="mean", value_domain="raw_code", front_end="log"
        )
    assert (
        output_rate(10e6, item_bytes=4, aggregation="mean", value_domain="linear", front_end="log")
        > 0
    )
    assert (
        output_rate(
            10e6, item_bytes=2, aggregation="mean", value_domain="raw_code", front_end="linear"
        )
        > 0
    )


# ---- board memory ---------------------------------------------------------------------------


def test_dual_channel_double_buffer_leaves_52_71_mib_of_the_512():
    frame = FrameSpec("raw", 7000, 8600, channels=2, bytes_per_sample=2, buffers=2)
    r = board_memory([frame], reserved_bytes=0, ddr_bytes=DDR)
    assert r.total_bytes == 481_600_000
    assert (DDR - r.total_bytes) / 1024**2 == pytest.approx(52.71, abs=0.01)
    assert r.verdict is Verdict.PASS


def test_the_documented_frame_sizes():
    one = board_memory([FrameSpec("a", 7000, 8600, 1, 2, 1)], reserved_bytes=0, ddr_bytes=DDR)
    assert one.total_bytes == 120_400_000 and one.total_bytes / 1024**2 == pytest.approx(
        114.82, abs=0.01
    )


def test_the_reserve_and_the_per_frame_overhead_count():
    frame = FrameSpec("raw", 7000, 8600, 2, 2, 2, overhead_fraction=0.05)
    r = board_memory([frame], reserved_bytes=67_108_864, ddr_bytes=DDR)
    assert r.total_bytes == pytest.approx(481_600_000 * 1.05)
    assert r.verdict is Verdict.FAIL, "505.7 MB + 64 MiB reserve exceeds 512 MiB"


def test_an_unknown_reserve_makes_the_memory_budget_unresolved():
    frame = FrameSpec("raw", 100, 100, 1, 2, 1)
    r = board_memory([frame], reserved_bytes=None, ddr_bytes=DDR)
    assert r.verdict is Verdict.UNRESOLVED and "reserved" in r.reason


def test_raw_reconstructed_and_float_frames_all_count_when_they_coexist():
    frames = [FrameSpec("raw", 4000, 5000, 1, 2, 2), FrameSpec("recon", 4000, 5000, 1, 4, 1)]
    r = board_memory(frames, reserved_bytes=0, ddr_bytes=DDR)
    assert r.total_bytes == 4000 * 5000 * (2 * 2 + 4)
