"""Package A reproduces what its author delivered (status: independent rerun)."""

import pytest

from zlscan.evidence import legacy_runner as lr
from zlscan.evidence.records import compare


def test_delivered_metrics_contain_every_checked_key():
    delivered = lr.delivered_metrics(lr.A_DELIVERED_METRICS)
    missing = [k for k in lr.A_KEYS if k not in delivered]
    assert missing == []
    assert delivered["environment"]["python"], "the author's environment must be on record"


@pytest.mark.slow
def test_demo_rerun_exits_cleanly(a_demo):
    result, metrics = a_demo
    assert result.returncode == 0, result.output[-2000:]
    assert metrics, "no metrics.json was produced"


@pytest.mark.slow
@pytest.mark.parametrize("key", lr.A_KEYS)
def test_demo_rerun_reproduces_the_author_reported_metric(a_demo, key):
    _, metrics = a_demo
    expected = lr.delivered_metrics(lr.A_DELIVERED_METRICS)[key]
    criterion = compare(key, expected, metrics.get(key), rel_tol=lr.FLOAT_REL_TOL)
    assert criterion["passed"], criterion


@pytest.mark.slow
def test_its_own_test_suite_passes(staged):
    result = lr.run_a_own_tests(staged / "package_a")
    assert result.returncode == 0, result.output[-2000:]
    assert lr.parse_pytest_passed(result.output) == lr.A_OWN_TESTS
