"""Photon and signal-level budgets: package B's documented numbers; no number from a gap."""

import math

import pytest

from zlscan.budget.photons import PhotonInputs, electrons_needed, photon_budget, sigma_density
from zlscan.budget.signals import SignalInputs, signal_budget
from zlscan.contracts.verdict import Verdict

# Package B's illustrative scenario (all values are its assumptions, not device data).
B = dict(
    laser_w=1e-3, pre_film_efficiency=0.4, collection_efficiency=0.00125, qe=0.15,
    gain=1000.0, wavelength_nm=635.0, pixel_time_s=100e-9, excess_noise_factor=1.3,
    anode_limit_a=1e-4,
)  # fmt: skip


def test_clear_film_photoelectrons_and_anode_current_match_the_documented_numbers():
    r = photon_budget(PhotonInputs(**B), densities=[0.0, 3.0])
    clear, dense = r.rows
    assert clear.electrons_per_pixel == pytest.approx(23_975, rel=1e-3)
    assert clear.anode_current_a == pytest.approx(38.4e-6, rel=2e-3)
    assert dense.electrons_per_pixel == pytest.approx(23.975, rel=1e-3)


def test_density_noise_at_d3_is_about_0_10_and_flagged_as_a_low_count_estimate():
    r = photon_budget(PhotonInputs(**B), densities=[3.0])
    (row,) = r.rows
    assert row.sigma_density == pytest.approx(0.1011, abs=2e-3)
    assert row.high_count_valid is False, "24 electrons is not in the high-count limit"


def test_electrons_needed_for_a_target_noise():
    assert electrons_needed(0.01, 1.3) == pytest.approx(2452, rel=1e-3)
    r = photon_budget(PhotonInputs(**B), densities=[0.0], target_sigma_density=0.01)
    assert r.electrons_needed == pytest.approx(2452, rel=1e-3)


def test_sigma_density_formula_and_validity_threshold():
    assert sigma_density(1e4, 1.0) == pytest.approx(1 / math.sqrt(1e4) / math.log(10))
    with pytest.raises(ValueError):
        sigma_density(0, 1.3)


def test_the_anode_limit_flags_an_over_current_clear_field():
    low = photon_budget(PhotonInputs(**{**B, "anode_limit_a": 1e-6}), densities=[0.0])
    assert low.rows[0].over_limit is True
    assert photon_budget(PhotonInputs(**B), densities=[0.0]).rows[0].over_limit is False


def test_a_missing_input_gives_no_number_only_a_list_of_what_is_missing():
    r = photon_budget(PhotonInputs(**{**B, "laser_w": None, "qe": None}), densities=[0.0, 3.0])
    assert r.status is Verdict.UNRESOLVED
    assert set(r.missing) == {"laser_w", "qe"}
    assert r.rows == [] and r.electrons_needed is None


def test_a_missing_noise_factor_blocks_only_the_noise_figures():
    r = photon_budget(PhotonInputs(**{**B, "excess_noise_factor": None}), densities=[0.0])
    assert r.rows[0].electrons_per_pixel == pytest.approx(23_975, rel=1e-3)
    assert r.rows[0].sigma_density is None
    assert "excess_noise_factor" in r.partial_missing


def test_a_default_is_never_substituted_for_an_unknown():
    all_unknown = PhotonInputs()
    r = photon_budget(all_unknown, densities=[0.0])
    assert r.rows == [] and len(r.missing) >= 6


@pytest.mark.parametrize("field", ["laser_w", "qe", "gain", "wavelength_nm", "pixel_time_s"])
def test_non_positive_inputs_are_rejected(field):
    with pytest.raises(ValueError, match=field):
        photon_budget(PhotonInputs(**{**B, field: 0.0}), densities=[0.0])


# ---- signal levels --------------------------------------------------------------------------

# Package B's demo_log analogue chain and ADC.
S = dict(log_offset_v=0.2, log_slope_v_per_decade=0.5, log_reference_current_a=40e-6, polarity=-1,
         driver_gain=1.0, driver_offset_v=0.0, adc_vmin=0.0, adc_vmax=2.5, adc_bits=16,
         clear_current_a=38.4e-6, dark_current_a=1e-9)  # fmt: skip


def test_the_density_step_per_adc_code_is_package_bs_7_63e_minus_5():
    r = signal_budget(SignalInputs(**S), densities=[0.0])
    assert r.density_step_per_code == pytest.approx(7.62939453125e-5, rel=1e-9)
    assert r.lsb_v == pytest.approx(2.5 / 65536)


def test_voltages_move_the_right_way_with_density_for_each_polarity():
    inverted = signal_budget(SignalInputs(**S), densities=[0.0, 1.0, 2.0])
    assert inverted.rows[0].adc_v < inverted.rows[1].adc_v < inverted.rows[2].adc_v
    direct = signal_budget(
        SignalInputs(**{**S, "polarity": +1, "log_offset_v": 1.5}), densities=[0.0, 1.0]
    )
    assert direct.rows[0].adc_v > direct.rows[1].adc_v


def test_a_level_outside_the_adc_range_is_reported_as_out_of_range():
    r = signal_budget(SignalInputs(**{**S, "adc_vmax": 0.3}), densities=[0.0, 3.0])
    assert [row.in_adc_range for row in r.rows] == [True, False]
    assert r.rows[1].margin_v < 0


def test_the_four_measurement_nodes_are_not_conflated():
    r = signal_budget(SignalInputs(**S), densities=[0.0])
    assert set(r.nodes) == {
        "log_board_output",
        "second_stage_output",
        "module_input",
        "chip_differential_input",
    }
    assert r.nodes["log_board_output"] is not None
    assert r.nodes["second_stage_output"] is None and r.nodes["module_input"] is None
    assert "未决" in r.node_notes["module_input"]


def test_a_missing_adc_range_is_unresolved_never_pass():
    r = signal_budget(SignalInputs(**{**S, "adc_vmax": None}), densities=[0.0])
    assert r.status is Verdict.UNRESOLVED and "adc_vmax" in r.missing
    assert r.rows == [] and r.density_step_per_code is None


def test_pole_time_constants_become_a_tail_in_pixels():
    r = signal_budget(
        SignalInputs(**S), densities=[0.0], pole_hz=[8e6, 3e6, 1.5e6], pixel_rate_hz=10e6
    )
    tau = sum(1 / (2 * math.pi * f) for f in (8e6, 3e6, 1.5e6))
    assert r.tail_pixels == pytest.approx(tau * 10e6)


def test_no_pixel_rate_means_no_tail_figure():
    r = signal_budget(SignalInputs(**S), densities=[0.0], pole_hz=[8e6])
    assert r.tail_pixels is None
