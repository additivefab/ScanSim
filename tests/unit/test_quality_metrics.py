import numpy as np
import pytest
from scipy.special import ndtr

from zlscan.contracts.flags import InvalidFlag
from zlscan.metrics.quality import (
    compare_density,
    facet_banding,
    geometry_error,
    slanted_edge_mtf,
    step_recovery,
    uniform_noise_psd,
    wedge_statistics,
)


def test_density_comparison_uses_common_finite_mask_and_decomposes_mse():
    density = np.array([[1.0, 2.0, 4.0], [5.0, 6.0, 7.0]])
    flags = np.array([[0, 0, InvalidFlag.MISSING_SAMPLE], [0, 0, 0]], dtype=np.uint16)
    truth = np.array([[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]])
    optical = np.array([[1.0, 0.0, 0.0], [np.nan, 0.0, 0.0]])
    region = np.array([[True, True, True], [True, False, False]])
    result = compare_density(density, flags, {"truth": truth, "optical": optical}, region)
    assert result.metrics["valid_fraction"] == 0.5
    assert result.metrics["invalid_reason_counts"]["MISSING_SAMPLE"] == 1
    assert result.metrics["references"]["truth"]["mse"] == 2.5
    assert result.metrics["references"]["optical"]["mse"] == 2
    for values in result.metrics["references"].values():
        assert values["mse"] == pytest.approx(values["bias"] ** 2 + values["variance"])


def test_white_noise_psd_is_flat_in_expectation():
    rng = np.random.default_rng(170)
    patches = rng.normal(size=(384, 16, 16))
    psd = uniform_noise_psd(patches, 0.001, 0.002)
    assert psd[0, 0] == pytest.approx(0, abs=1e-30)
    np.testing.assert_allclose(psd.ravel()[1:], 2e-6, rtol=0.28)


@pytest.mark.parametrize("transpose", [False, True])
def test_slanted_edge_recovers_gaussian_response(transpose):
    y, x = np.mgrid[0:128, 0:128]
    distance = (x - 64 - y / 8) * 1e-4
    sigma = 1.5e-4
    image = ndtr(distance / sigma)
    mask = np.abs(distance) <= 0.002
    if transpose:
        image, distance, mask = image.T, distance.T, mask.T
    frequencies, mtf = slanted_edge_mtf(image, distance, mask, 1.25e-5)
    keep = frequencies < 2000
    theory = np.exp(-2 * (np.pi * sigma * frequencies[keep]) ** 2)
    np.testing.assert_allclose(mtf[keep], theory, atol=0.015)


def test_wedge_bias_banding_grid_and_recovery_have_known_answers():
    wedge = np.array([[0.1, 1.1, 2.1], [0.1, 1.1, 2.1]])
    labels = np.tile(np.arange(3), (2, 1))
    stats = wedge_statistics(wedge, labels, np.arange(3), np.ones(wedge.shape, dtype=bool))
    assert stats["linearity"]["slope"] == pytest.approx(1)
    assert stats["highest_step_mean_od"] == pytest.approx(2.1)

    band = facet_banding(
        np.array([1, 3, 1, 3]),
        np.array([0, 1, 0, 1]),
        np.array([1, 3, 1, 3]),
    )
    assert band["facet_level_offsets"] == {"0": -1.0, "1": 1.0}
    assert band["edge_roughness_rms_m"] == 0

    target = np.array([[0, 0], [0, 1], [0, 2]])
    actual = target + np.array([[0, 0], [0.1, 0], [0.2, 0]])
    assert geometry_error(actual, target, np.arange(3))["scan_skew_dx_dy"] == pytest.approx(0.1)

    signal = np.array([0, 0, 0.5, 0.9, 1, 1, 1])
    assert step_recovery(signal, 2)["settling_samples"] == 2
