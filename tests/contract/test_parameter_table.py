"""The unified parameter table: complete, honest about unknowns, and free of forbidden defaults."""

import csv
import importlib.util
import json
import math
from pathlib import Path

import pytest

from zlscan.contracts.param import Evidence, Param

ROOT = Path(__file__).resolve().parents[2]
LEGACY = ROOT / "legacy"
EVIDENCE = ROOT / "src" / "zlscan" / "evidence"

# Every parameter that carries a value, and why. Adding a value anywhere else must come with
# an edit here, which is the point: values have to be justified one by one.
KNOWN_VALUES = {
    "adc.module_silkscreen_name": Evidence.PHOTO,
    "adc.module_silkscreen_bits": Evidence.PHOTO,
    "adc.module_silkscreen_channels": Evidence.PHOTO,
    "adc.module_silkscreen_max_msps": Evidence.PHOTO,
    "board.family": Evidence.PHOTO,
    "board.revision": Evidence.PHOTO,
    "board.fpga_schematic_part": Evidence.SCHEMATIC,
    "board.vccio_bank35_v": Evidence.SCHEMATIC,
    "board.pl_oscillator_nominal_hz": Evidence.SCHEMATIC,
    "board.ps_oscillator_nominal_hz": Evidence.SCHEMATIC,
    "board.ddr_physical_bytes": Evidence.SCHEMATIC,
    "transport.phy": Evidence.SCHEMATIC,
    "transport.phy_connection": Evidence.SCHEMATIC,
    "observation.scope_model": Evidence.PHOTO,
    "observation.scope_analog_channels": Evidence.PHOTO,
    "benchmark.width_pixels": Evidence.ASSUMED,
    "benchmark.height_pixels": Evidence.ASSUMED,
    "benchmark.wall_time_goal_s": Evidence.ASSUMED,
    "benchmark.host_peak_memory_goal_bytes": Evidence.ASSUMED,
}

# docs/dev/01_project_brief.md, section 4: unknown, so they must stay None.
MUST_BE_UNKNOWN = [
    "laser.wavelength_nm", "laser.power_w", "scan.facet_count", "scan.rpm", "scan.line_rate_hz",
    "scan.line_period_s", "scan.f_theta_focal_mm", "optics.spot_psf", "pmt.model",
    "afe.second_stage_gain_offset", "afe.log_amp_device", "adc.actual_conversion_rate_hz",
    "adc.clock_source", "adc.digital_interface_mode", "adc.input_coupling", "adc.register_dump",
    "adc.channel_role_a", "adc.channel_role_b", "control.dac_route", "control.sos_source",
    "control.encoder_present", "control.facet_index_sensor_present",
    "control.reference_photodetector_present", "hardware.wiring_carrier_module_to_board",
    "product.film_size_mm", "product.pixel_pitch_um", "product.density_range_and_tolerance",
    "product.scan_and_transfer_time_s", "board.vccio_bank33_measured_v",
]  # fmt: skip


@pytest.fixture(scope="module")
def table():
    return json.loads((EVIDENCE / "parameters.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def entries(table):
    return table["parameters"]


@pytest.fixture(scope="module")
def by_key(entries):
    return {e["key"]: e for e in entries}


@pytest.fixture(scope="module")
def forbidden():
    return json.loads((EVIDENCE / "forbidden_defaults.json").read_text(encoding="utf-8"))["claims"]


# ---- structure -----------------------------------------------------------------------------


def test_every_entry_is_a_valid_param(entries):
    for entry in entries:
        Param.model_validate(entry["param"])


def test_ids_and_keys_are_unique(entries):
    assert len({e["id"] for e in entries}) == len(entries)
    assert len({e["key"] for e in entries}) == len(entries)


def test_committed_table_is_the_generator_output():
    spec = importlib.util.spec_from_file_location(
        "build_parameter_table", ROOT / "tools" / "build_parameter_table.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    current = (EVIDENCE / "parameters.json").read_text(encoding="utf-8")
    assert current == module.render(module.build()), "run tools/build_parameter_table.py"


# ---- coverage: every source row has a destination -------------------------------------------


def _csv_rows(relative):
    with (LEGACY / relative).open(encoding="utf-8-sig", newline="") as handle:
        return [row["参数"] for row in csv.DictReader(handle)]


def _referenced(entries, table_name):
    return {s["row"] for e in entries for s in e["sources"] if s["table"] == table_name}


def test_every_row_of_package_a_table_has_a_destination(entries):
    rows = _csv_rows("package_a/docs/参数证据与测量清单.csv")
    assert len(rows) == 19
    assert set(rows) <= _referenced(entries, "A")


def test_every_row_of_package_b_table_has_a_destination(entries):
    rows = _csv_rows("package_b/templates/实测参数记录表.csv")
    assert len(rows) == 16
    assert set(rows) <= _referenced(entries, "B")


def test_every_review_requirement_has_a_destination(entries):
    requirements = json.loads(
        (LEGACY / "review/tables/requirements.json").read_text(encoding="utf-8")
    )
    assert {r["id"] for r in requirements} == {f"R0{i}" for i in range(1, 9)}
    assert {r["id"] for r in requirements} <= _referenced(entries, "R")


def test_every_hardware_draft_leaf_has_a_destination(entries, table):
    draft = json.loads(
        (LEGACY / "review/configs/硬件配置草案_未知项禁止自动补齐.json").read_text(encoding="utf-8")
    )

    def leaves(obj, prefix=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                yield from leaves(v, f"{prefix}.{k}" if prefix else k)
        else:
            yield prefix

    elsewhere = set(table["draft_leaves_not_parameters"])
    missing = set(leaves(draft)) - elsewhere - _referenced(entries, "D")
    assert missing == set()


def test_requirement_text_is_copied_from_the_source_not_retyped(by_key):
    reason = json.loads((LEGACY / "review/tables/requirements.json").read_text(encoding="utf-8"))[
        0
    ]["reason"]
    assert any(a["text"] == reason for a in by_key["product.film_size_mm"]["affects"])


# ---- honesty about unknowns ----------------------------------------------------------------


def test_only_the_listed_parameters_carry_a_value(entries):
    with_value = {
        e["key"]: Evidence(e["param"]["evidence"])
        for e in entries
        if e["param"]["value"] is not None
    }
    assert with_value == KNOWN_VALUES


@pytest.mark.parametrize("key", MUST_BE_UNKNOWN)
def test_unknowns_stay_none(by_key, key):
    param = by_key[key]["param"]
    assert param["value"] is None and param["evidence"] is None


def test_example_values_of_the_legacy_packages_are_kept_apart_from_values(by_key):
    laser = by_key["laser.wavelength_nm"]
    assert laser["param"]["value"] is None
    assert laser["legacy_examples"], "the packages' example must still be on record"
    assert any("635" in ex["text"] for ex in laser["legacy_examples"])


def test_assumed_values_are_only_benchmark_targets_and_are_pending(entries):
    assumed = [e for e in entries if e["param"]["evidence"] == "assumed"]
    assert {e["category"] for e in assumed} == {"benchmark"}
    assert all(e["param"]["status"] == "pending" for e in assumed)


def test_the_silkscreen_rate_is_labelled_as_a_marking_not_an_operating_point(by_key):
    param = by_key["adc.module_silkscreen_max_msps"]["param"]
    assert param["value"] == 125 and "不是实测工作点" in param["valid_for"]
    assert by_key["adc.actual_conversion_rate_hz"]["param"]["value"] is None


# ---- forbidden defaults --------------------------------------------------------------------


def _matches(rule, value):
    expected = rule["value"]
    if value is None:
        return False
    if expected == "*":
        return True
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        return math.isclose(value, expected, rel_tol=rule.get("rel_tol", 0.0), abs_tol=0.0)
    return value == expected


def test_no_forbidden_value_appears_without_a_measurement(by_key, forbidden):
    offenders = []
    for claim in forbidden:
        for rule in claim["rules"]:
            param = by_key[rule["key"]]["param"]
            if _matches(rule, param["value"]) and param["evidence"] != "measured":
                offenders.append((claim["id"], rule["key"], param["value"]))
    assert offenders == []


def test_every_rule_names_a_real_parameter(by_key, forbidden):
    stale = [(c["id"], r["key"]) for c in forbidden for r in c["rules"] if r["key"] not in by_key]
    assert stale == []


def test_the_matcher_really_catches_a_forbidden_value(by_key, forbidden):
    """Guard against a matcher that never fires: feed it the very values it should reject."""
    f01 = next(c for c in forbidden if c["id"] == "F01")
    assert any(_matches(rule, 0.0007) for rule in f01["rules"])
    assert any(_matches(rule, 0.00071) for rule in f01["rules"])  # within 5 %
    assert not any(_matches(rule, 0.002) for rule in f01["rules"])
    f08 = next(c for c in forbidden if c["id"] == "F08")
    assert _matches(f08["rules"][0], "some board") and not _matches(f08["rules"][0], None)


def test_a_forbidden_value_with_measured_evidence_would_be_allowed_but_assumed_would_not():
    rule = {"key": "scan.facet_count", "value": 6}
    assert _matches(rule, 6)
    # The table-level test above rejects it unless evidence == "measured".
    assert Param(value=6, unit="", evidence=Evidence.MEASURED, source_id="M01",
                 measured_at="2026-11-11", method="counted facets").value == 6  # fmt: skip


def test_the_draft_non_facts_are_all_covered(forbidden):
    draft = json.loads(
        (LEGACY / "review/configs/硬件配置草案_未知项禁止自动补齐.json").read_text(encoding="utf-8")
    )
    covered = {c["draft_non_fact"] for c in forbidden if "draft_non_fact" in c}
    assert set(draft["explicit_non_facts"]) <= covered


def test_text_only_claims_are_the_five_unmeasured_conclusions(forbidden):
    text_only = {c["id"] for c in forbidden if not c["rules"]}
    assert text_only == {"F13", "F14", "F15", "F16", "F17"}
    assert len(forbidden) == 17, "docs/dev/07_evidence_rules.md section 2 lists 17 claims"
