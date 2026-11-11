"""Package B reproduces what its author delivered (status: independent rerun)."""

import pytest

from zlscan.evidence import legacy_runner as lr
from zlscan.evidence.records import compare

# The package's own ideal-regression test asserts this threshold.
IDEAL_REGRESSION_LIMIT = 3e-5


@pytest.mark.parametrize("config", lr.B_CONFIGS)
def test_delivered_metrics_contain_every_checked_key(config):
    delivered = lr.delivered_metrics(lr.B_DELIVERED[config])
    missing = [k for k in lr.B_KEYS if k not in delivered]
    assert missing == []


@pytest.mark.slow
def test_simulation_rerun_exits_cleanly(b_run):
    config, result, metrics = b_run
    assert result.returncode == 0, f"{config}: {result.output[-2000:]}"
    assert metrics, f"{config}: no metrics.json was produced"


@pytest.mark.slow
@pytest.mark.parametrize("key", lr.B_KEYS)
def test_simulation_rerun_reproduces_the_author_reported_metric(b_run, key):
    config, _, metrics = b_run
    expected = lr.delivered_metrics(lr.B_DELIVERED[config])[key]
    criterion = compare(f"{config}.{key}", expected, metrics.get(key), rel_tol=lr.FLOAT_REL_TOL)
    assert criterion["passed"], criterion


@pytest.mark.slow
@pytest.mark.parametrize("b_run", ["ideal_regression"], indirect=True)
def test_ideal_regression_stays_below_the_packages_own_limit(b_run):
    _, _, metrics = b_run
    assert metrics["density_rmse_unblurred"] < IDEAL_REGRESSION_LIMIT


@pytest.mark.slow
def test_its_own_test_suite_passes(staged):
    result = lr.run_b_own_tests(staged / "package_b")
    assert result.returncode == 0, result.output[-2000:]
    assert lr.parse_unittest_ran(result.output) == lr.B_OWN_TESTS
