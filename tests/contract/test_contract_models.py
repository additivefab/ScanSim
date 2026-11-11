"""Round trips and rules for the remaining contract models."""

import pytest
from pydantic import ValidationError

from zlscan.contracts.calibration import CalibrationHeader, Provenance
from zlscan.contracts.events import Derivation, Event, Origin, Visibility
from zlscan.contracts.flags import InvalidFlag
from zlscan.contracts.module import Module, ModuleSpec
from zlscan.contracts.query import QueryStatus
from zlscan.contracts.signals import SignalDecl
from zlscan.contracts.time import SampleId
from zlscan.contracts.verdict import Verdict

# ---- events ---------------------------------------------------------------------------------


def event(**over):
    base = dict(
        kind="SOS", origin=Origin.SIMULATED, visibility=Visibility.OBSERVABLE,
        derivation=Derivation.SENSOR, capability_ref="hardware.control.sos_sensor",
        clock_domain=0, count=7, tick=123_456, session_id=1, epoch=0, uncertainty_ticks=None,
        related_sample=None, payload={},
    )  # fmt: skip
    base.update(over)
    return Event(**base)


def test_event_round_trips_through_json():
    e = event(related_sample=SampleId("r", "main", 1, 0, 0, 0, 5), payload={"edge": "rising"})
    assert Event.model_validate_json(e.model_dump_json()) == e


def test_the_three_evidence_fields_are_independent():
    sim_observable = event(origin=Origin.SIMULATED, visibility=Visibility.OBSERVABLE)
    oracle = event(
        visibility=Visibility.ORACLE_ONLY, derivation=Derivation.INFERRED, capability_ref=None
    )
    assert (
        sim_observable.origin is Origin.SIMULATED
        and sim_observable.visibility is Visibility.OBSERVABLE
    )
    assert oracle.visibility is Visibility.ORACLE_ONLY and oracle.origin is Origin.SIMULATED


def test_an_observable_sensor_event_needs_a_capability():
    with pytest.raises(ValidationError, match="capability_ref"):
        event(capability_ref=None)


def test_an_inferred_or_command_event_needs_no_sensor_capability():
    event(derivation=Derivation.INFERRED, capability_ref=None)
    event(derivation=Derivation.COMMAND, capability_ref=None)


def test_unknown_event_fields_are_rejected():
    with pytest.raises(ValidationError):
        event(facet_id_true=3)


def test_time_is_an_integer_tick_not_a_float():
    with pytest.raises(ValidationError):
        event(tick=1.5)


def test_only_observable_events_pass_the_reconstruction_gate():
    assert event().recon_verdict() is Verdict.PASS
    oracle = event(
        visibility=Visibility.ORACLE_ONLY, derivation=Derivation.INFERRED, capability_ref=None
    )
    assert oracle.recon_verdict() is Verdict.FAIL


# ---- calibration header ---------------------------------------------------------------------


def header(**over):
    base = dict(
        format_version=1, provenance=Provenance.SIMULATED, calibration_id="c1",
        created_at="2026-11-11T00:00:00Z",
        binds_to=dict(hardware_hash="h", wiring_rev="w", firmware_hash="f", register_dump_hash="r",
                      channel_roles={"0": "signal"}, gain_settings={"pmt": "g1"},
                      epoch_range=[0, 3]),
        valid_conditions={"scan_direction": "forward"},
        generator={"name": "calib", "version": "0.0"},
    )  # fmt: skip
    base.update(over)
    return CalibrationHeader(**base)


def test_calibration_header_round_trips():
    h = header()
    assert CalibrationHeader.model_validate_json(h.model_dump_json()) == h


def test_a_model_calibration_is_refused_unless_explicitly_allowed():
    h = header(provenance=Provenance.MODEL)
    assert h.accepted_for_reconstruction(allow_model=False) is False
    assert h.accepted_for_reconstruction(allow_model=True) is True
    assert header(provenance=Provenance.SIMULATED).accepted_for_reconstruction(allow_model=False)
    assert header(provenance=Provenance.MEASURED).accepted_for_reconstruction(allow_model=False)


def test_a_calibration_binds_to_hardware_epoch_and_gain():
    h = header()
    session = dict(hardware_hash="h", wiring_rev="w", firmware_hash="f", register_dump_hash="r",
                   gain_settings={"pmt": "g1"}, epoch=2)  # fmt: skip
    assert h.matches_session(**session)
    assert not h.matches_session(**{**session, "hardware_hash": "other"})
    assert not h.matches_session(**{**session, "gain_settings": {"pmt": "g2"}})
    assert not h.matches_session(**{**session, "epoch": 9})


def test_an_unidentifiable_item_carries_no_value():
    with pytest.raises(ValidationError, match="unidentifiable"):
        header(items={"geometry_y": {"status": "unidentifiable", "value": 0.0}})
    header(items={"geometry_y": {"status": "unidentifiable", "value": None}})


# ---- signals, flags, query status -----------------------------------------------------------


def test_signal_declaration_round_trips_and_defaults_are_conservative():
    s = SignalDecl(
        signal_id="afe.v_log", component_id="afe", port_id="out", physical_domain="analog",
        quantity="voltage", unit="V", value_type="scalar", origin=Origin.SIMULATED,
        visibility=Visibility.ORACLE_ONLY, derivation=Derivation.SENSOR,
        parameter_evidence="assumed", model_id="afe.log", model_version="0",
        clock_domain=0, native_step_policy="fine_step",
    )  # fmt: skip
    assert SignalDecl.model_validate_json(s.model_dump_json()) == s
    assert s.display_interpolation == "none" and s.recording_policy == "on_demand"


def test_invalid_flags_have_the_documented_bit_positions():
    assert [int(f) for f in (
        InvalidFlag.NONPOSITIVE_TRANSMITTANCE, InvalidFlag.WHITE_REFERENCE_INVALID,
        InvalidFlag.OUTSIDE_CALIBRATION, InvalidFlag.ADC_SATURATED, InvalidFlag.FRONT_END_CLIPPED,
        InvalidFlag.MISSING_SAMPLE, InvalidFlag.RESAMPLING_SUPPORT,
        InvalidFlag.CALIBRATION_MISMATCH,
    )] == [1, 2, 4, 8, 16, 32, 64, 128]  # fmt: skip
    combined = InvalidFlag.ADC_SATURATED | InvalidFlag.MISSING_SAMPLE
    assert (
        InvalidFlag.MISSING_SAMPLE in combined
        and InvalidFlag.NONPOSITIVE_TRANSMITTANCE not in combined
    )


def test_query_statuses_are_the_six_documented_ones():
    assert {s.value for s in QueryStatus} == {
        "exact_record", "replayed", "interpolated", "summary_only", "not_recorded", "not_computed",
    }  # fmt: skip


# ---- module spec and protocol ---------------------------------------------------------------


def test_module_spec_round_trips_and_demands_a_fidelity_level():
    spec = ModuleSpec(
        name="afe.lowpass", interface={"in": "V", "out": "V"}, state={"y": "V"},
        equation="y' = (x - y) / tau", parameters={"tau": "s"}, applicability="f << 1/(2 pi dt)",
        fidelity="L1", validation=["test_lowpass_dc"],
    )  # fmt: skip
    assert ModuleSpec.model_validate_json(spec.model_dump_json()) == spec
    with pytest.raises(ValidationError):
        ModuleSpec(**{**spec.model_dump(), "fidelity": "L9"})


def test_the_module_protocol_names_the_five_operations():
    for op in ("initialize", "advance", "checkpoint", "restore", "observables"):
        assert hasattr(Module, op)
