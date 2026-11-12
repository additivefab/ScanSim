import numpy as np
import pytest

from zlscan.experiment.grid_statistics import equivalence, summarize, summarize_fraction


def test_large_uncertainty_is_not_equivalence_even_when_means_match():
    result = equivalence([-10, 10, -10, 10], [-10, 10, -10, 10], 0.1, alpha=0.05)
    assert result["estimate"] == 0
    assert result["status"] == "inconclusive"


def test_shared_calibration_error_cancels_only_in_declared_paired_comparison():
    a = np.array([1, 8, 2, 7, 5])
    paired = equivalence(a + 0.001, a, 0.01, alpha=0.05, paired=True)
    independent = equivalence(a + 0.001, a, 0.01, alpha=0.05)
    assert paired["status"] == "equivalent"
    assert independent["status"] == "inconclusive"
    assert paired["ci"][0] == pytest.approx(0.001)


def test_clear_difference_and_missing_data_are_not_passes():
    assert (
        equivalence([2, 2, 2], [0, 0, 0], 0.1, alpha=0.05)["status"] == "outside_equivalence_band"
    )
    assert equivalence([None, 1], [1, 1], 0.1, alpha=0.05)["status"] == "insufficient_replicates"
    assert summarize([None, None]) == {"n": 0, "mean": None, "ci": None}
    assert summarize([None, 1])["ci"] is None


def test_log_power_ratio_is_compared_in_declared_relative_band():
    result = equivalence([1.01] * 5, [1] * 5, np.log(1.25), alpha=0.05, log_ratio=True)
    assert result["status"] == "equivalent"
    assert result["ratio_estimate"] == pytest.approx(1.01)
    with pytest.raises(ValueError, match="positive"):
        equivalence([0, 0], [1, 1], 0.2, alpha=0.05, log_ratio=True)


def test_zero_observed_invalid_counts_do_not_imply_zero_uncertainty():
    result = equivalence([0] * 8, [0] * 8, 0.02, alpha=0.05 / 48, bounded_fraction=True)
    assert result["status"] == "inconclusive"
    assert result["ci"][0] < 0 < result["ci"][1]
    summary = summarize_fraction([0] * 8)
    assert summary["mean"] == 0
    assert summary["ci"][0] == 0 and summary["ci"][1] > 0
