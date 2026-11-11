"""The claims in docs/reports/legacy_cross_compare.md, checked against the committed data files.

The data files are produced by tools/legacy_cross_compare.py and tools/legacy_experiments.py
(reproducible; see the evidence records). These tests fail if the report's statements and the
data ever diverge.
"""

import json
from pathlib import Path

import pytest

REPORTS = Path(__file__).resolve().parents[2] / "docs" / "reports"


@pytest.fixture(scope="module")
def steady():
    return json.loads((REPORTS / "legacy_cross_compare_data.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def experiments():
    return json.loads((REPORTS / "legacy_experiments_data.json").read_text(encoding="utf-8"))


def test_the_two_packages_agree_along_the_whole_dc_chain(steady):
    assert [row["density"] for row in steady["rows"]] == [0.0, 1.0, 2.0]
    for row in steady["rows"]:
        for key, rel in row["relative_difference"].items():
            if key == "code":
                assert row["absolute_difference"]["code"] == 0, "ADC codes must be identical"
            else:
                assert abs(rel) < 1e-6, (row["density"], key, rel)


def test_the_polarity_alignment_used_is_recorded(steady):
    assert steady["conditions"]["slope"] > 0, "package B is run with the negated slope"


def test_step_response_differs_by_less_than_one_percent_of_the_swing(experiments):
    step = experiments["step_response"]
    assert step["rms_difference_v"] < 0.01 * step["voltage_swing_v"]
    # No time offset between the packages: the earlier 51 ns / 40 % figures were a method error
    # (each trace was zeroed on its own first valid sample).
    assert abs(step["best_time_shift_of_b_ns"]) <= 2.0


def test_alignment_explains_most_of_package_bs_squared_error(experiments):
    for name, case in experiments["registration_ablation_package_b"].items():
        assert case["oracle"]["mse"] < 0.25 * case["as_is"]["mse"], name
        assert 2.0 <= case["oracle_shift_px"] <= 2.5, name


def test_the_error_is_variance_not_bias_and_not_noise(experiments):
    ablation = experiments["registration_ablation_package_b"]
    for case in ablation.values():
        assert abs(case["as_is"]["bias"]) < 0.05 * case["as_is"]["rmse"]
    for config in ("ad9268_50m_5x", "demo_log"):
        noisy = ablation[f"{config}/noise"]["as_is"]["mse"]
        clean = ablation[f"{config}/noiseless"]["as_is"]["mse"]
        assert abs(noisy - clean) < 0.05 * clean, "switching noise off barely changes the error"


def test_cost_per_fine_step_is_on_record_for_both_packages(experiments):
    cost = experiments["cost_per_step"]
    assert set(cost) == {"package_a_demo", "package_b_ad9268"}
    assert all(100 < c["ns_per_substep"] < 1000 for c in cost.values())
