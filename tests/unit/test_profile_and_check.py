"""Hardware profile gate, the parameter-table loader, the event gate and ``zlscan check``."""

import pytest

from zlscan.cli import CHECK_FAIL_EXIT_CODE, CHECK_UNRESOLVED_EXIT_CODE, main
from zlscan.contracts.event_gate import recon_event_gate
from zlscan.contracts.param import Evidence, Param
from zlscan.contracts.verdict import Verdict
from zlscan.evidence.table import load_parameters
from zlscan.hardware.profile import REQUIRED_FOR_HARDWARE, ProfileStatus, profile_gate

# ---- loader ---------------------------------------------------------------------------------


def test_loader_returns_a_param_per_key():
    params = load_parameters()
    assert len(params) == 95
    assert all(isinstance(p, Param) for p in params.values())
    assert params["board.revision"].value == "V1.3B"
    assert params["laser.wavelength_nm"].value is None


# ---- profile gate ---------------------------------------------------------------------------


def test_the_required_list_names_real_parameters_and_has_fifteen_items():
    params = load_parameters()
    assert len(REQUIRED_FOR_HARDWARE) == 15
    assert [k for k, _ in REQUIRED_FOR_HARDWARE if k not in params] == []


def test_every_required_item_is_reported_missing_for_the_draft_table():
    result = profile_gate(load_parameters())
    assert result.status is ProfileStatus.UNRESOLVED
    assert len(result.missing) == 15
    assert "ADC 实际转换速率" in " ".join(result.missing)


def _filled(params):
    out = dict(params)
    for key, _ in REQUIRED_FOR_HARDWARE:
        out[key] = Param(
            value=True,
            unit="",
            evidence=Evidence.MEASURED,
            source_id="M01",
            measured_at="2026-11-11",
            method="test fixture",
        )
    return out


def test_with_everything_supplied_the_gate_only_says_ready_for_further_validation():
    result = profile_gate(_filled(load_parameters()))
    assert result.missing == []
    assert result.status is ProfileStatus.READY_FOR_FURTHER_VALIDATION
    assert result.status != Verdict.PASS, "complete data is not a pass"
    assert "不等于" in result.note, "completeness is not electrical or timing verification"


def test_an_unverified_flag_counts_as_missing_even_though_it_is_not_none():
    params = _filled(load_parameters())
    params["hardware.wiring_carrier_module_to_board"] = Param(
        value=False,
        unit="",
        evidence=Evidence.MEASURED,
        source_id="M01",
        measured_at="2026-11-11",
        method="continuity check found a mismatch",
    )
    result = profile_gate(params)
    assert result.status is ProfileStatus.UNRESOLVED
    assert len(result.missing) == 1


def test_an_assumed_value_does_not_satisfy_the_gate():
    params = _filled(load_parameters())
    params["adc.actual_conversion_rate_hz"] = Param(
        value=125e6, unit="Hz", evidence=Evidence.ASSUMED
    )
    assert len(profile_gate(params).missing) == 1


# ---- event gate -----------------------------------------------------------------------------

EVENT = {
    "kind": "SOS",
    "origin": "SIMULATED",
    "visibility": "OBSERVABLE",
    "derivation": "SENSOR",
    "capability_ref": "hardware.control.sos_sensor",
    "clock_domain": 0,
    "count": 1,
    "tick": 5,
    "session_id": 1,
    "epoch": 0,
}


def test_a_complete_observable_event_passes():
    assert recon_event_gate(EVENT) is Verdict.PASS


def test_the_review_packages_single_observability_field_is_not_accepted():
    old = {"observability": "measured", "origin": "x", "type": "SOS"}
    assert recon_event_gate(old) is Verdict.FAIL


def test_missing_origin_is_unresolved():
    event = {k: v for k, v in EVENT.items() if k != "origin"}
    assert recon_event_gate(event) is Verdict.UNRESOLVED


def test_a_sensor_event_without_a_capability_is_unresolved():
    event = {k: v for k, v in EVENT.items() if k != "capability_ref"}
    assert recon_event_gate(event) is Verdict.UNRESOLVED


def test_an_inferred_event_needs_no_sensor_capability():
    event = {k: v for k, v in EVENT.items() if k != "capability_ref"}
    assert recon_event_gate({**event, "derivation": "INFERRED"}) is Verdict.PASS


@pytest.mark.parametrize("field,value", [("visibility", "oracle_only"), ("origin", "TRUTH")])
def test_unknown_enum_values_fail(field, value):
    assert recon_event_gate({**EVENT, field: value}) is Verdict.FAIL


# ---- zlscan check ---------------------------------------------------------------------------


def test_check_reports_unresolved_and_lists_what_is_missing(capsys):
    code = main(["check"])
    out = capsys.readouterr().out
    assert code == CHECK_UNRESOLVED_EXIT_CODE
    assert "未决" in out and "15" in out
    assert "ADC 实际转换速率" in out
    assert "120" in out, "the pin registry size is reported"
    assert "不是接线批准" in out


def test_check_never_exits_zero_while_data_is_missing():
    assert main(["check"]) != 0


def test_exit_codes_are_distinct():
    assert len({0, 2, 3, CHECK_FAIL_EXIT_CODE, CHECK_UNRESOLVED_EXIT_CODE}) == 5
