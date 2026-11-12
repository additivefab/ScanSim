import numpy as np
import pytest

from zlscan.calib.geometry import fit_facet_geometry, infer_facet_phase


def test_missing_line_sync_loses_phase_until_an_observable_anchor():
    ticks = np.array([0, 100, 201, 400, 500, 600, 700], dtype=np.int64)
    phase = infer_facet_phase(ticks, 3, 100, 3, anchors={0: 1, 5: 2})
    np.testing.assert_array_equal(phase, [1, 2, 0, -1, -1, 2, 0])
    assert np.all(infer_facet_phase(ticks, 3, 100, 3) == -1)


def test_facet_geometry_recovers_injected_offsets_distortion_and_gain():
    t = np.tile(np.linspace(-1, 1, 12), 2)
    facet = np.repeat([0, 1], 12)
    expected = [(0.001, 0.02, 0.0002), (-0.002, 0.021, -0.0003)]
    x = np.zeros_like(t)
    for f, (offset, scale, cubic) in enumerate(expected):
        chosen = facet == f
        x[chosen] = offset + scale * t[chosen] + cubic * t[chosen] ** 3
    dy = np.where(facet == 0, 0.001, -0.002)
    slope = 0.25
    slanted_x = x + slope * dy
    intensity = np.where(facet == 0, 0.9, 1.1)
    results = fit_facet_geometry(
        facet,
        t,
        x,
        slanted_x - x,
        slope,
        intensity,
    )
    for f in (0, 1):
        fit = results[f]
        assert fit.x_coefficients_m == pytest.approx(expected[f], abs=1e-12)
        assert fit.x_residual_rms_m < 1e-12
        assert fit.y_offset_m == pytest.approx(dy[f * 12])
        assert fit.gain == pytest.approx(intensity[f * 12])

    uniform = fit_facet_geometry(facet, t, x, None, None, intensity)
    assert uniform[0].y_offset_m is None
    assert uniform[0].y_status == "unidentifiable"
