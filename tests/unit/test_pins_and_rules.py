"""Pin registry model and the wiring rules that stop a mistaken connection from "passing"."""

import hashlib
import json
from pathlib import Path

import pytest

from zlscan.contracts.verdict import Verdict
from zlscan.hardware.pins import PinRegistry, PinRow
from zlscan.hardware.rules import (
    Assignment,
    check_assignments,
    check_bank_voltage,
    overall_verdict,
    table_revision_for,
)

ROOT = Path(__file__).resolve().parents[2]
NEW = "V1.3/V1.3B"
OLD = "V1.0/V1.1"


@pytest.fixture(scope="module")
def reg():
    return PinRegistry.load()


# ---- the registry itself --------------------------------------------------------------------


def test_the_imported_registry_is_byte_identical_to_the_frozen_review_table():
    imported = (ROOT / "src/zlscan/hardware/pin_registry.json").read_bytes()
    frozen = (ROOT / "legacy/review/tables/pin_registry.json").read_bytes()
    assert hashlib.sha256(imported).hexdigest() == hashlib.sha256(frozen).hexdigest()


def test_three_tables_of_forty_pins(reg):
    assert sorted(reg.tables()) == [(OLD, "J5"), (NEW, "J5"), (NEW, "J6")]
    assert all(len(reg.table(rev, conn)) == 40 for rev, conn in reg.tables())


def test_each_table_has_six_non_signal_pins(reg):
    for rev, conn in reg.tables():
        roles = [r.net_role for r in reg.table(rev, conn)]
        assert roles.count("5V_DC") == 2 and roles.count("VCC3V3") == 2 and roles.count("GND") == 2


def test_signals_have_a_package_pin_and_power_pins_do_not(reg):
    for row in reg.rows:
        if row.net_role == "GPIO":
            assert row.package_pin and row.logical_index is not None
        else:
            assert row.package_pin == "" and row.logical_index is None


def test_logical_indices_run_0_to_33_in_physical_pin_order(reg):
    for rev, conn in reg.tables():
        signals = sorted(reg.signal_pins(rev, conn), key=lambda r: r.physical_pin)
        assert [r.logical_index for r in signals] == list(range(34))


def test_logical_index_is_not_the_physical_pin(reg):
    """The confusion the review warned about: J5[0] sits on physical pin 7 of the new board."""
    assert reg.resolve_logical(NEW, "J5", 0).physical_pin == 7
    assert reg.resolve_logical(NEW, "J6", 0).physical_pin == 1
    assert reg.resolve_logical(OLD, "J5", 0).physical_pin == 1


def test_the_two_j5_revisions_disagree_on_the_power_pins(reg):
    assert reg.get(NEW, "J5", 1).net_role == "5V_DC" and reg.get(OLD, "J5", 1).net_role == "GPIO"
    assert reg.get(NEW, "J5", 39).net_role == "GPIO" and reg.get(OLD, "J5", 39).net_role == "5V_DC"


def test_the_registry_never_proposes_an_adc_wiring(reg):
    assert {r.adc_assignment for r in reg.rows} == {"未确定；不是ADC接线建议"}


def test_a_malformed_registry_is_rejected(reg):
    rows = [r.model_dump() for r in reg.rows]
    rows[0]["physical_pin"] = 2  # duplicates pin 2 of the same table
    with pytest.raises(ValueError, match="duplicate"):
        PinRegistry([PinRow(**r) for r in rows])
    rows = [r.model_dump() for r in reg.rows if not (r.connector == "J6" and r.physical_pin == 40)]
    with pytest.raises(ValueError, match="40 pins"):
        PinRegistry([PinRow(**r) for r in rows])


def test_unknown_fields_in_a_row_are_rejected():
    data = json.loads((ROOT / "src/zlscan/hardware/pin_registry.json").read_text(encoding="utf-8"))
    data[0]["colour"] = "red"
    with pytest.raises(ValueError):
        PinRow(**data[0])


# ---- revisions ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "board,table",
    [("V1.3", NEW), ("V1.3B", NEW), ("V1.0", OLD), ("V1.1", OLD), ("V1.2", None), ("V9", None)],
)
def test_board_revision_to_pin_table(board, table):
    assert table_revision_for(board) == table


# ---- wiring rules (T-HW negative cases) -----------------------------------------------------


def codes(findings):
    return sorted(f.code for f in findings)


def test_a_valid_signal_pin_raises_no_finding(reg):
    assert (
        check_assignments(reg, "V1.3B", [Assignment("D0", "J5", 7, expected_package_pin="H19")])
        == []
    )


def test_old_j5_table_used_on_the_new_board_fails(reg):
    findings = check_assignments(
        reg, "V1.3B", [Assignment("D0", "J5", 1, table_revision=OLD, expected_package_pin="H19")]
    )
    assert "wrong_revision_table" in codes(findings)
    assert all(f.verdict is Verdict.FAIL for f in findings)


def test_a_power_pin_assigned_to_data_fails(reg):
    findings = check_assignments(reg, "V1.3B", [Assignment("D0", "J5", 1)])
    assert codes(findings) == ["non_signal_pin"]


def test_a_ground_pin_assigned_to_data_fails(reg):
    ground = next(r for r in reg.table(NEW, "J6") if r.net_role == "GND")
    assert codes(
        check_assignments(reg, "V1.3B", [Assignment("D0", "J6", ground.physical_pin)])
    ) == ["non_signal_pin"]


def test_logical_index_taken_for_a_physical_pin_is_caught(reg):
    # Someone reads "J5[0] = H19" and wires physical pin 0+1: pin 1 is 5 V on the new board.
    findings = check_assignments(
        reg, "V1.3B", [Assignment("D0", "J5", 1, expected_package_pin="H19")]
    )
    assert {"non_signal_pin", "package_pin_mismatch"} <= set(codes(findings))


def test_a_wrong_expected_package_pin_fails(reg):
    findings = check_assignments(
        reg, "V1.3B", [Assignment("D0", "J5", 7, expected_package_pin="H20")]
    )
    assert codes(findings) == ["package_pin_mismatch"]


def test_a_pin_that_does_not_exist_fails(reg):
    assert codes(check_assignments(reg, "V1.3B", [Assignment("D0", "J5", 41)])) == ["no_such_pin"]
    assert codes(check_assignments(reg, "V1.3B", [Assignment("D0", "J9", 1)])) == ["no_such_pin"]


def test_two_signals_on_one_pin_fail(reg):
    findings = check_assignments(
        reg, "V1.3B", [Assignment("D0", "J5", 7), Assignment("D1", "J5", 7)]
    )
    assert codes(findings) == ["double_assigned"]


def test_an_unlisted_board_revision_is_unresolved_not_assumed(reg):
    findings = check_assignments(reg, "V1.2", [Assignment("D0", "J5", 7)])
    assert codes(findings) == ["revision_not_covered"]
    assert findings[0].verdict is Verdict.UNRESOLVED


def test_no_findings_is_not_a_pass(reg):
    findings = check_assignments(reg, "V1.3B", [Assignment("D0", "J5", 7)])
    assert findings == []
    assert overall_verdict(findings, wiring_verified=None) is Verdict.UNRESOLVED
    assert overall_verdict(findings, wiring_verified=False) is Verdict.UNRESOLVED
    assert overall_verdict(findings, wiring_verified=True) is Verdict.PASS


def test_any_failure_wins_even_when_wiring_is_marked_verified(reg):
    findings = check_assignments(reg, "V1.3B", [Assignment("D0", "J5", 1)])
    assert overall_verdict(findings, wiring_verified=True) is Verdict.FAIL


# ---- bank voltage ---------------------------------------------------------------------------


def test_j5_is_fixed_at_3v3():
    assert check_bank_voltage("J5", io_standard_v=3.3).verdict is Verdict.PASS
    assert check_bank_voltage("J5", io_standard_v=1.8).verdict is Verdict.FAIL


def test_j6_with_unknown_vadj_is_unresolved_whatever_the_nominal_says():
    assert (
        check_bank_voltage("J6", io_standard_v=3.3, measured_vadj_v=None).verdict
        is Verdict.UNRESOLVED
    )


def test_j6_compares_against_the_measured_vadj():
    assert check_bank_voltage("J6", io_standard_v=3.3, measured_vadj_v=3.3).verdict is Verdict.PASS
    assert check_bank_voltage("J6", io_standard_v=1.8, measured_vadj_v=3.3).verdict is Verdict.FAIL
    assert check_bank_voltage("J6", io_standard_v=1.8, measured_vadj_v=1.8).verdict is Verdict.PASS


def test_an_unknown_connector_is_unresolved():
    assert check_bank_voltage("J7", io_standard_v=3.3).verdict is Verdict.UNRESOLVED
