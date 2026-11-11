"""The 20 rule tests of the review package, ported (docs/dev/stages/stage0.md, S0-T06).

Each test carries the id it had in legacy/review/scripts/test_review_checks.py. The rules now
live in the modules where they belong:

* 01-14  data-path budget helpers            -> zlscan.budget.datapath
* 15-16  oscilloscope channel gate           -> zlscan.observation.gate
* 17-18  observable-event gate for the       -> zlscan.contracts.event_gate
         reconstruction (rewritten for the V4.1 origin/visibility/derivation fields; the
         review used a single 'observability' field)
* 19     hardware profile gate               -> zlscan.hardware.profile
* 20     pin registry versions and roles     -> zlscan.hardware.pins
"""

import math

import pytest

from zlscan.budget.datapath import GateResult, bandwidth, frame_bytes, queue_trace
from zlscan.contracts.event_gate import recon_event_gate
from zlscan.contracts.verdict import Verdict
from zlscan.evidence.table import load_parameters
from zlscan.hardware.pins import PinRegistry
from zlscan.hardware.profile import ProfileStatus, profile_gate
from zlscan.observation.gate import CaptureGate, measurement_gate

# ---- 01-12: bandwidth and frame sizes (continuous-mode average gate) ------------------------


def test_01_dual_125_msps_raw_rate():
    assert bandwidth(125e6, 2).raw_bytes_per_s == 500_000_000


def test_02_single_20_msps_raw_rate():
    assert bandwidth(20e6, 1).raw_bytes_per_s == 40_000_000


def test_03_over_rate_fails():
    assert bandwidth(125e6, 2, output_bytes_per_s=125e6).average_gate is GateResult.FAIL


def test_04_no_output_figure_is_unresolved():
    assert bandwidth(20e6, 1).average_gate is GateResult.UNRESOLVED


def test_05_equal_rate_has_no_margin_so_fails():
    assert bandwidth(50e6, 1, output_bytes_per_s=100e6).average_gate is GateResult.FAIL


def test_06_a_keep_window_is_not_free_margin():
    result = bandwidth(
        125e6, 2, keep_fraction=0.2, overhead_fraction=0.05, output_bytes_per_s=100e6
    )
    assert result.average_gate is GateResult.FAIL


def test_07_decimation_divides_the_kept_rate():
    assert bandwidth(40e6, 2, decimation=2).kept_bytes_per_s == 80_000_000


def test_08_a_nan_rate_is_rejected():
    with pytest.raises(ValueError):
        bandwidth(math.nan, 1)


def test_09_a_keep_fraction_above_one_is_rejected():
    with pytest.raises(ValueError):
        bandwidth(20e6, 1, keep_fraction=1.2)


def test_10_single_channel_frame_size():
    assert frame_bytes(7000, 8600, 1, 2) == 120_400_000


def test_11_dual_channel_frame_size():
    assert frame_bytes(7000, 8600, 2, 2) == 240_800_000


def test_12_a_zero_dimension_is_rejected():
    with pytest.raises(ValueError):
        frame_bytes(7000, 0, 1, 2)


# ---- 13-14: queue conservation --------------------------------------------------------------


def test_13_queue_conservation():
    q = queue_trace([("arrival", 10), ("service", 4), ("arrival", 8), ("service", 5)], 12)
    assert q == {"produced": 18, "consumed": 9, "lost": 2, "queued": 7, "peak": 12}


def test_14_unused_service_is_not_saved_for_later():
    assert queue_trace([("service", 100), ("arrival", 10)], 20)["queued"] == 10


# ---- 15-16: two-channel oscilloscope --------------------------------------------------------


def test_15_two_scope_channels_reject_three_simultaneous_signals():
    assert measurement_gate(3, 2) is CaptureGate.FAIL


def test_16_repeated_captures_are_not_simultaneous():
    assert measurement_gate(3, 2, simultaneous=False) is CaptureGate.NON_SIMULTANEOUS


# ---- 17-18: events handed to the reconstruction ---------------------------------------------

VALID_EVENT = {
    "kind": "SOS",
    "origin": "SIMULATED",
    "visibility": "OBSERVABLE",
    "derivation": "SENSOR",
    "capability_ref": "hardware.control.sos_sensor",
    "clock_domain": 0,
    "count": 7,
    "tick": 123_456,
    "session_id": 1,
    "epoch": 0,
}


def test_17_a_true_facet_id_smuggled_into_an_event_is_blocked():
    leaky = {**VALID_EVENT, "facet_id_true": 3}
    assert recon_event_gate(leaky) is Verdict.FAIL


def test_18_oracle_only_events_are_blocked():
    assert recon_event_gate({**VALID_EVENT, "visibility": "ORACLE_ONLY"}) is Verdict.FAIL


# ---- 19: hardware profile gate --------------------------------------------------------------


def test_19_the_profile_stays_unresolved_while_hardware_data_is_missing():
    result = profile_gate(load_parameters())
    assert result.status is ProfileStatus.UNRESOLVED
    assert len(result.missing) == 15, "the review package listed exactly 15 missing items"


# ---- 20: pin registry versions and roles ----------------------------------------------------


def test_20_pin_registry_versions_and_roles():
    registry = PinRegistry.load()
    assert len(registry.rows) == 120
    for revision, connector in [("V1.3/V1.3B", "J5"), ("V1.3/V1.3B", "J6"), ("V1.0/V1.1", "J5")]:
        table = registry.table(revision, connector)
        assert sum(r.net_role == "GPIO" for r in table) == 34
        assert len({r.physical_pin for r in table}) == 40
    assert registry.get("V1.3/V1.3B", "J5", 1).net_role == "5V_DC"
    assert registry.get("V1.0/V1.1", "J5", 1).package_pin == "H19"
    assert registry.get("V1.3/V1.3B", "J5", 7).package_pin == "H19"
    assert registry.get("V1.3/V1.3B", "J6", 1).package_pin == "U22"
