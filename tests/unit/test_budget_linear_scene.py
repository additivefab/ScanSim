"""Linear front-end signal budget and scene-driven density levels (N1)."""

import json
import math

import pytest

from zlscan.budget.runner import run_budget
from zlscan.budget.signals import LinearSignalInputs, linear_signal_budget
from zlscan.contracts.verdict import Verdict
from zlscan.engine.research_config import load_scenario, scenario_budget

L = dict(
    offset_v=0.0, transimpedance_ohm=1e4, driver_gain=1.0, driver_offset_v=0.0, adc_vmin=0.0,
    adc_vmax=1.0, adc_bits=16, clear_current_a=5e-5, dark_current_a=0.0,
)  # fmt: skip


def test_linear_levels_codes_and_density_step():
    r = linear_signal_budget(LinearSignalInputs(**L), [0.0, 2.0, 4.0])
    assert r.status is Verdict.PASS
    lsb = 1.0 / 65536
    assert r.lsb_v == pytest.approx(lsb)
    assert [row.adc_v for row in r.rows] == pytest.approx([0.5, 0.005, 0.00005])
    assert all(row.in_adc_range for row in r.rows)
    assert r.rows[2].codes_above_dark == pytest.approx(0.00005 / lsb)
    # d(D) = dV / (ln10 * signal): one code is a growing density step as the signal falls.
    assert r.rows[2].density_step_per_code == pytest.approx(lsb / (math.log(10) * 0.00005))


def test_linear_out_of_range_is_flagged_not_clamped():
    r = linear_signal_budget(LinearSignalInputs(**{**L, "clear_current_a": 1.7e-4}), [0.0, 1.0])
    assert r.rows[0].adc_v == pytest.approx(1.7)
    assert not r.rows[0].in_adc_range and r.rows[0].margin_v == pytest.approx(-0.7)
    assert r.rows[1].in_adc_range


def test_linear_unknown_inputs_give_no_numbers():
    r = linear_signal_budget(LinearSignalInputs(**{**L, "transimpedance_ohm": None}), [0.0])
    assert r.status is Verdict.UNRESOLVED and r.missing == ["transimpedance_ohm"] and not r.rows


def test_linear_dark_offset_is_excluded_from_signal_codes():
    r = linear_signal_budget(LinearSignalInputs(**{**L, "dark_current_a": 1e-6}), [3.0])
    assert r.rows[0].adc_v == pytest.approx(1e4 * (5e-8 + 1e-6))
    assert r.rows[0].codes_above_dark == pytest.approx(1e4 * 5e-8 * 65536)


def _scenario(tmp_path, afe, density):
    doc = {
        "format": "zlscan.research-scenario/1", "model_version": "chain-0.3", "preset": "nominal",
        "lines": 4, "chain": {"afe": afe},
        "scenes": {"sample": {"width_m": 0.08, "height_m": 0.08, "off_film_T": 1.0,
                              "density": density}},
    }  # fmt: skip
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return load_scenario(path)


def test_scenario_budget_covers_scene_maximum_density(tmp_path):
    cfg = scenario_budget(_scenario(tmp_path, {}, [[0.5, 4.5]]))
    assert cfg["densities"] == [0.0, 1.0, 2.0, 3.0, 4.0, 4.5]
    ids = {c["id"] for c in run_budget(cfg)["conclusions"]}
    assert {"photons.electrons.D4.5", "photons.sigma.D4"} <= ids


def test_scenario_budget_keeps_default_levels_for_thin_scene(tmp_path):
    assert scenario_budget(_scenario(tmp_path, {}, [[0.2, 1.5]]))["densities"] == [
        0.0, 1.0, 2.0, 3.0,
    ]  # fmt: skip


def test_scenario_budget_linear_front_end_rows(tmp_path):
    afe = {"kind": "linear", "v0": 0.0, "r_t_ohm": 10000.0}
    result = run_budget(scenario_budget(_scenario(tmp_path, afe, [[0.5, 4.5]])))
    rows = {c["id"]: c for c in result["conclusions"]}
    assert "signals.levels" not in rows
    assert rows["signals.adc_v.D0"]["status"] in {"PASS", "FAIL"}
    assert rows["signals.codes.D4.5"]["status"] == "INFO"
    assert rows["signals.codes.D4.5"]["value"] == pytest.approx(
        rows["signals.codes.D0"]["value"] * 10**-4.5
    )
    assert "budget.transimpedance_ohm" in result["assumed_inputs"]
