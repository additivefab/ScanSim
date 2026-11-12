import numpy as np
import pytest

from zlscan.calib.radiometric import fit_log_transfer


@pytest.mark.parametrize("slope,offset", [(6200.0, 25000.0), (-5400.0, 34000.0)])
def test_wedge_fit_recovers_injected_slope_and_intercept(slope, offset):
    density = np.array([0.0, 0.5, 1.0, 1.5, 2.0])
    transmittance = 10**-density
    dark_ratio = 0.003
    dark = np.full((5, 200), round(offset + slope * np.log10(dark_ratio)))
    white = np.full((5, 200), round(offset + slope * np.log10(1 + dark_ratio)))
    wedge = np.broadcast_to(
        np.repeat(np.rint(offset + slope * np.log10(transmittance + dark_ratio)), 40),
        dark.shape,
    )
    # A narrow scratch in the white reference leaves a warning and preserves the robust fit.
    white[:, 81:83] += 1500
    dark[:, 4] += 100
    dark[:, 5] -= 100
    fit = fit_log_transfer(dark, white, wedge, density)
    assert abs(fit.slope_codes_per_decade - slope) <= fit.slope_uncertainty_codes_per_decade
    assert abs(fit.intercept_code - offset) <= fit.intercept_uncertainty_code
    assert fit.white_outlier_columns == (81, 82)
    assert fit.dark_ratio == pytest.approx(dark_ratio, rel=0.002)
    assert fit.code_knots[0] < fit.code_knots[-1]
    assert fit.code_knots[0] <= np.min(dark) <= np.max(dark) <= fit.code_knots[-1]


def test_flat_references_cannot_identify_transfer():
    flat = np.full((4, 100), 2000)
    with pytest.raises(ValueError, match="contrast"):
        fit_log_transfer(flat, flat, flat, np.linspace(0, 2, 5))
