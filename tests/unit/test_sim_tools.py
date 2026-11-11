"""Parsing of Icarus/vvp output and ngspice wrdata files, plus the analytic reference curves."""

import math

import numpy as np
import pytest

from zlscan.evidence import sim_tools as st

# ---- vvp output ----------------------------------------------------------------------------


def test_pass_lines_are_collected():
    out = "PASS: first thing\nsome noise\nPASS: second thing\n"
    parsed = st.parse_vvp(out)
    assert parsed["pass_lines"] == ["PASS: first thing", "PASS: second thing"]
    assert parsed["error_lines"] == []


@pytest.mark.parametrize(
    "line",
    [
        "ERROR: tb.sv:31: ADC/tag mismatch",
        "FATAL: whatever",
        "error: lowercase also counts",
        "Watchdog timeout",
    ],
)
def test_error_lines_are_collected(line):
    parsed = st.parse_vvp(f"PASS: a\n{line}\n")
    assert parsed["error_lines"] == [line]


def test_a_pass_in_the_middle_of_a_line_is_not_a_pass_line():
    assert st.parse_vvp("not PASS: nope\n")["pass_lines"] == []


# ---- wrdata files --------------------------------------------------------------------------


def test_reads_plain_numeric_columns(tmp_path):
    path = tmp_path / "w.txt"
    path.write_text(" 0.0e+00  1.0e+00\n 1.0e-09  2.0e+00\n", encoding="utf-8")
    names, data = st.read_wrdata(path)
    assert names is None
    assert data.shape == (2, 2)
    assert data[1, 0] == 1e-9 and data[1, 1] == 2.0


def test_reads_a_header_line_of_vector_names(tmp_path):
    path = tmp_path / "w.txt"
    path.write_text("time v(ideal) v(lp)\n0 1 2\n1e-9 3 4\n", encoding="utf-8")
    names, data = st.read_wrdata(path)
    assert names == ["time", "v(ideal)", "v(lp)"]
    assert data.shape == (2, 3)


def test_an_empty_file_is_an_error(tmp_path):
    path = tmp_path / "w.txt"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="no numeric"):
        st.read_wrdata(path)


def test_ragged_rows_are_an_error(tmp_path):
    path = tmp_path / "w.txt"
    path.write_text("1 2\n3\n", encoding="utf-8")
    with pytest.raises(ValueError, match="column"):
        st.read_wrdata(path)


def test_interpolation_picks_the_value_at_a_time():
    t = np.array([0.0, 1.0, 2.0])
    v = np.array([0.0, 10.0, 20.0])
    assert st.value_at(t, v, 1.5) == pytest.approx(15.0)


# ---- analytic references -------------------------------------------------------------------


def test_rc_highpass_decay_after_a_step():
    tau = 1e3 * 100e-9  # 1 kOhm, 100 nF
    assert st.rc_highpass_step(t=1e-3 + tau, t0=1e-3, step=0.5, tau=tau) == pytest.approx(
        0.5 * math.exp(-1)
    )
    assert st.rc_highpass_step(t=0.5e-3, t0=1e-3, step=0.5, tau=tau) == 0.0


def test_log_stage_static_voltage_matches_the_netlist_formula():
    # B's netlist: V = VOFF + SLOPE*log10(IREF/I), IREF = 40 uA, SLOPE = 0.5, VOFF = 0.2.
    v = st.log_stage_voltage(current=40e-6, v_off=0.2, slope=0.5, i_ref=40e-6)
    assert v == pytest.approx(0.2)
    assert st.log_stage_voltage(40e-9, 0.2, 0.5, 40e-6) == pytest.approx(0.2 + 0.5 * 3)


# ---- tools ---------------------------------------------------------------------------------


def test_a_missing_tool_is_reported_as_missing_not_as_an_error():
    assert st.find_tool("definitely-not-a-real-simulator-binary") is None
