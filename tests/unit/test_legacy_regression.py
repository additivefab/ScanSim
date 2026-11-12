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
