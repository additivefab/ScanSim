"""Budget runner: hardware draft stays unresolved; legacy presets reproduce documented numbers."""

import json
from pathlib import Path

import pytest

from zlscan.budget.runner import BudgetRefused, render_report, run_budget, write_outputs
from zlscan.cli import main

ROOT = Path(__file__).resolve().parents[2]


def preset(name):
    return json.loads((ROOT / "configs" / f"{name}.json").read_text(encoding="utf-8"))


def by_id(result):
    return {c["id"]: c for c in result["conclusions"]}


def test_the_hardware_draft_computes_nothing():
    result = run_budget()
    assert result["conclusions"]
    for c in result["conclusions"]:
        assert c["status"] == "UNRESOLVED" and c["value"] is None, c["id"]
        assert "无法计算" in c["text"]


def test_hardware_constrained_refuses_the_draft_and_names_what_is_missing():
    with pytest.raises(BudgetRefused) as exc:
        run_budget({"mode": "hardware_constrained"})
    assert any("scan.rpm" in p and "缺少" in p for p in exc.value.problems)


def test_hardware_constrained_refuses_assumed_inputs_even_when_complete():
    cfg = preset("legacy_b")
    cfg["mode"] = "hardware_constrained"
    with pytest.raises(BudgetRefused) as exc:
        run_budget(cfg)
    assert any("仅为假设" in p for p in exc.value.problems)


def test_legacy_b_reproduces_the_documented_numbers_and_is_flagged_assumed():
    r = run_budget(preset("legacy_b"))
    c = by_id(r)
    assert r["flagged_assumed_scenario"] and r["assumed_inputs"]
    assert c["geometry.pixel_rate_hz"]["value"] == pytest.approx(8.378e6, rel=1e-3)
    assert c["geometry.frame_time_s"]["value"] == pytest.approx(8.6)
    assert c["photons.electrons.D0"]["value"] == pytest.approx(23_975, rel=1e-3)
    assert c["photons.anode.D0"]["value"] == pytest.approx(38.4e-6, rel=2e-3)
    assert c["photons.sigma.D3"]["value"] == pytest.approx(0.1011, abs=2e-3)
    assert c["signals.density_step"]["value"] == pytest.approx(7.62939453125e-5)
    assert c["memory.board"]["value"] == 481_600_000
    assert c["memory.board"]["status"] == "PASS"
    assert all(x["assumed_inputs"] > 0 for x in r["conclusions"] if x["value"] is not None)


def test_the_10mhz_preset_needs_the_238_732_mm_focal_length():
    r = run_budget(preset("legacy_b_10mhz"))
    assert by_id(r)["geometry.pixel_rate_hz"]["value"] == pytest.approx(10e6, rel=1e-5)


def test_a_value_without_evidence_is_rejected():
    with pytest.raises(ValueError, match="evidence"):
        run_budget({"inputs": {"scan.rpm": {"value": 1000}}})


def test_report_marks_the_assumed_scenario_first_and_lists_evidence():
    page = render_report(run_budget(preset("legacy_b")))
    assert page.index("假设场景") < page.index("<table>")
    assert "[assumed]" in page


def test_the_draft_report_has_no_number_in_the_value_column():
    page = render_report(run_budget())
    assert "无法计算" in page and " Hz" not in page


def test_cli_writes_json_and_report_and_exit_codes(tmp_path, capsys):
    assert main(["budget", "--out", str(tmp_path / "a")]) == 4
    assert (tmp_path / "a" / "budget.json").exists()
    cfg = ROOT / "configs" / "legacy_b.json"
    code = main(["budget", "--config", str(cfg), "--out", str(tmp_path / "b")])
    assert code in (0, 4)
    assert main(["budget", "--mode", "hardware_constrained", "--out", str(tmp_path / "c")]) == 1
    assert not (tmp_path / "c").exists()
    capsys.readouterr()


def test_write_outputs_round_trips(tmp_path):
    j, h = write_outputs(run_budget(), tmp_path)
    assert json.loads(j.read_text(encoding="utf-8"))["schema"] == "zlscan.budget/1"
    assert h.read_text(encoding="utf-8").startswith("<!doctype html>")
