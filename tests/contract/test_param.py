"""Param: a value that carries its evidence. Unknown stays None; evidence cannot be invented."""

import pytest
from pydantic import ValidationError

from zlscan.contracts.param import Evidence, Param


def test_unknown_parameter_has_no_value_and_no_evidence():
    p = Param(unit="nm")
    assert p.value is None and p.evidence is None
    assert p.status == "pending"


def test_value_without_evidence_is_rejected():
    with pytest.raises(ValidationError, match="evidence"):
        Param(value=635, unit="nm")


def test_evidence_without_value_is_rejected():
    with pytest.raises(ValidationError, match="evidence"):
        Param(evidence=Evidence.ASSUMED, unit="nm")


def test_zero_is_a_value_not_an_unknown():
    p = Param(value=0, unit="V", evidence=Evidence.ASSUMED)
    assert p.value == 0 and p.evidence is Evidence.ASSUMED


@pytest.mark.parametrize(
    "evidence",
    [Evidence.PROJECT_TEXT, Evidence.PHOTO, Evidence.SCHEMATIC, Evidence.DATASHEET],
)
def test_cited_evidence_needs_a_source(evidence):
    with pytest.raises(ValidationError, match="source_id"):
        Param(value=1, unit="V", evidence=evidence)
    assert Param(value=1, unit="V", evidence=evidence, source_id="S05").source_id == "S05"


def test_assumed_and_derived_values_need_no_source():
    Param(value=1, unit="V", evidence=Evidence.ASSUMED)
    Param(value=1, unit="V", evidence=Evidence.DERIVED)


def test_a_measured_value_needs_its_measurement_conditions():
    with pytest.raises(ValidationError, match="measured_at"):
        Param(value=1, unit="V", evidence=Evidence.MEASURED, source_id="M01")
    ok = Param(
        value=1,
        unit="V",
        evidence=Evidence.MEASURED,
        source_id="M01",
        measured_at="2026-11-11",
        method="scope CH2, 1x probe, DC coupling",
    )
    assert ok.method


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError):
        Param(unit="V", colour="red")


def test_round_trip_through_json_keeps_none():
    p = Param(unit="W", valid_for="clear film, 20 C")
    again = Param.model_validate_json(p.model_dump_json())
    assert again == p and again.value is None


def test_status_is_restricted():
    with pytest.raises(ValidationError):
        Param(unit="V", status="maybe")


def test_evidence_enum_values_match_the_contract_document():
    assert {e.value for e in Evidence} == {
        "project_text",
        "photo_observation",
        "schematic_nominal",
        "datasheet",
        "derived",
        "assumed",
        "measured",
    }
