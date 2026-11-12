"""The V5 forward chain reproduces package B's delivered deterministic ideal frame."""

import pytest

from zlscan.metrics.legacy_regression import OWN_TEST_LIMIT, run_ideal_regression


@pytest.mark.legacy
def test_new_chain_matches_the_delivered_ideal_frame():
    result = run_ideal_regression()
    assert result.codes_shape == (16, 64)
    assert result.differing_codes == 0
    assert result.rmse_new < OWN_TEST_LIMIT  # package B's own acceptance limit
    # Reuse the legacy float metric comparison tolerance from S0-T03.
    assert result.rmse_new == pytest.approx(result.rmse_delivered, rel=1e-12, abs=0)


@pytest.mark.legacy
def test_new_chain_matches_the_package_a_ideal_frame():
    from zlscan.metrics.legacy_a_regression import run_ideal_a_regression

    result = run_ideal_a_regression()
    assert result.codes == 65_536
    assert result.differing_codes == 0
    # The legacy reconstruction interpolates in physical coordinates before averaging.
    assert result.rmse_v5 == pytest.approx(result.rmse_legacy, abs=1e-12, rel=0)
