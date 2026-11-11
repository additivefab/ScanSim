"""Fault injection: every fault is detected at run time or reported as not distinguishable."""

import pytest

from zlscan.experiment.faults import FAULTS, INFO, run_case


def test_a_clean_run_raises_no_alarm(tmp_path):
    out = run_case(None, workdir=tmp_path)
    assert out.fired == [] and out.verdict == "not_distinguishable" and not out.injected


@pytest.mark.parametrize("fault", FAULTS)
def test_each_fault_is_detected_with_a_known_pattern_and_distinct_channels(fault, tmp_path):
    out = run_case(fault, workdir=tmp_path)
    assert out.injected and out.info is INFO[fault]
    assert out.verdict == "detected", (fault, out.fired)


def test_a_misplaced_overflow_flag_needs_a_rail_code_to_be_seen(tmp_path):
    out = run_case("overflow_misaligned", rail_in_stimulus=False, workdir=tmp_path)
    assert out.verdict == "not_distinguishable" and "rail" in out.enhancement


def test_identical_stimulus_makes_a_channel_swap_not_distinguishable(tmp_path):
    out = run_case(
        "wrong_channel", distinct_channels=False, test_pattern_known=True, workdir=tmp_path
    )
    assert out.verdict == "not_distinguishable"
    assert "different test codes" in out.enhancement


def test_different_channel_patterns_make_the_swap_visible(tmp_path):
    out = run_case("wrong_channel", distinct_channels=True, workdir=tmp_path)
    assert out.verdict == "detected"


@pytest.mark.parametrize("fault", ["wrong_edge", "wrong_bit_order", "bit_flip", "buffer_wrap"])
def test_without_a_known_pattern_data_faults_go_unseen(fault, tmp_path):
    out = run_case(fault, test_pattern_known=False, distinct_channels=False, workdir=tmp_path)
    assert out.verdict == "not_distinguishable"
    assert out.enhancement


def test_structural_faults_are_seen_without_any_known_pattern(tmp_path):
    cases = [
        ("missing_line_sync", "line_spacing"),
        ("reset_interruption", "file_integrity"),
        ("extra_line_sync", "ignored_sync_counter"),
        ("clock_offset", "line_spacing"),
    ]
    for fault, check in cases:
        out = run_case(fault, test_pattern_known=False, distinct_channels=False, workdir=tmp_path)
        assert check in out.fired, (fault, out.fired)


def test_every_fault_has_a_complete_detectability_row():
    for fault in FAULTS:
        row = INFO[fault]
        for value in (row.location, row.checker, row.needs, row.checkpoint, row.observable_flag,
                      row.latency, row.blind_spot, row.enhancement):  # fmt: skip
            assert value
