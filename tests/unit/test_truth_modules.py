"""Fixed properties of each physical module (physics spec sections 1-7)."""

import math

import numpy as np
import pytest

from zlscan.engine.filters import FirstOrder
from zlscan.engine.rng import CounterRng
from zlscan.truth import adc, afe, collect, psf, scan, source
from zlscan.truth.scene import Scene, edge, step_wedge, uniform

# ---- scene ----------------------------------------------------------------------------------


def test_scene_constant_T():
    s = uniform(1.5, 0.1, 0.1)
    x = np.linspace(0.001, 0.099, 50)
    assert np.allclose(s.lookup(x, np.full_like(x, 0.05)), 10**-1.5)


def test_scene_off_film_rule():
    s = uniform(2.0, 0.1, 0.1)
    assert s.lookup(np.array([-0.01, 0.2]), np.array([0.05, 0.05])).tolist() == [1.0, 1.0]
    blocked = Scene.from_density(np.full((4, 4), 2.0), 0.1, 0.1, off_film_T=0.0)
    assert blocked.lookup(np.array([-0.01]), np.array([0.05]))[0] == 0.0


def test_scene_interpolates_transmittance_not_density():
    s = edge(0.0, 3.0, 0.1, 0.1, n=2)
    mid = s.lookup(np.array([0.05]), np.array([0.05]))[0]
    assert mid == pytest.approx((1 + 10**-3) / 2)  # not 10 ** -1.5


def test_negative_density_is_rejected():
    with pytest.raises(ValueError):
        Scene.from_density(np.array([[-0.1]]), 0.1, 0.1)


# ---- psf ------------------------------------------------------------------------------------


def test_psf_leaves_a_constant_transmittance_unchanged():
    s = uniform(1.0, 0.1, 0.1)
    out = psf.effective_transmittance(
        s.lookup, psf.PsfConfig(2e-3, 2e-3), np.array([0.05]), np.array([0.05])
    )
    assert out[0] == pytest.approx(0.1, rel=1e-12)


def test_blurring_transmittance_differs_from_blurring_density():
    s = edge(0.0, 3.0, 0.1, 0.1, n=512)
    x = np.array([0.05])
    y = np.array([0.05])
    t = psf.effective_transmittance(s.lookup, psf.PsfConfig(4e-3, 1e-9), x, y)[0]
    density_blur = 10 ** -(0.5 * 0 + 0.5 * 3)  # what blurring D to the mean would give
    assert t > density_blur * 10


def test_psf_nodes_converge():
    s = edge(0.0, 2.0, 0.1, 0.1, n=1024)
    x, y = np.array([0.0505]), np.array([0.05])
    vals = [psf.effective_transmittance(s.lookup, psf.PsfConfig(3e-3, 1e-9, nodes_x=n), x, y)[0]
            for n in (5, 9, 17, 33)]  # fmt: skip
    assert abs(vals[3] - vals[2]) < abs(vals[1] - vals[0]) + 1e-12


# ---- collect, source ------------------------------------------------------------------------


def test_collect_rolloff_is_parabolic_and_symmetric():
    c = collect.CollectConfig(0.4, 0.02, rolloff=0.1)
    u = np.array([0.0, 0.5, 1.0])
    e = collect.efficiency(c, u)
    assert (
        e[1] == pytest.approx(0.008)
        and e[0] == pytest.approx(e[2])
        and e[0] == pytest.approx(0.0072)
    )


def test_source_ripple_and_gating():
    cfg = source.SourceConfig(power_w=1.0, ripple=0.1, ripple_hz=1000.0)
    t = np.linspace(0, 1e-3, 5)
    k = np.zeros(5, dtype=np.int64)
    on = np.array([True, True, False, True, True])
    p = source.power(cfg, t, k, CounterRng(1), 1, on, np.ones(5, dtype=bool))
    assert p[2] == 0.0 and p[0] == pytest.approx(1.0)
    assert p[1] == pytest.approx(1.1)


def test_source_line_fluctuation_is_chunk_independent():
    cfg = source.SourceConfig(power_w=1.0, line_sigma=0.05)
    t = np.zeros(6)
    k = np.array([0, 0, 1, 1, 2, 2])
    full = source.power(cfg, t, k, CounterRng(7), 1, np.ones(6, bool), np.ones(6, bool))
    part = source.power(cfg, t[2:], k[2:], CounterRng(7), 1, np.ones(4, bool), np.ones(4, bool))
    assert np.array_equal(full[2:], part)


# ---- scan -----------------------------------------------------------------------------------

SC = scan.ScanConfig(facets=6, rpm=10000, focal_m=0.238732, pitch_x_m=50e-6, pitch_y_m=50e-6,
                     nx=7000, x_center_m=0.175)  # fmt: skip


def test_scan_geometry():
    assert SC.f_line == 1000
    assert SC.v_x == pytest.approx(500.0, rel=1e-4)
    assert SC.v_y == pytest.approx(0.05)
    assert SC.active_s == pytest.approx(700e-6, rel=1e-4)


def test_x_grows_linearly_inside_the_window_and_y_follows_the_feed():
    t = np.linspace(SC.window_start_s() + 1e-6, SC.window_start_s() + 100e-6, 50)
    k, x, y, facet, inside = scan.geometry(SC, t, CounterRng(1), 1)
    assert inside.all() and np.allclose(np.diff(x, 2), 0, atol=1e-12)
    assert np.allclose(y, SC.v_y * t)


def test_facets_cycle_and_facet_offsets_apply():
    cfg = scan.ScanConfig(facets=3, rpm=1200, focal_m=0.1, pitch_x_m=1e-3, pitch_y_m=1e-3, nx=10,
                          x_center_m=0.0, facet_offset_m=(0.0, 1e-3, 2e-3))  # fmt: skip
    t = (np.arange(6) + 0.5) / cfg.f_line + cfg.window_start_s() - 0.5 / cfg.f_line + 1e-6
    k, x, y, facet, inside = scan.geometry(cfg, t, CounterRng(1), 1)
    assert facet.tolist() == [0, 1, 2, 0, 1, 2]
    assert x[1] - x[0] == pytest.approx(1e-3, abs=1e-6)


def test_line_sync_instants_are_not_aligned_to_the_adc_clock():
    cfg = scan.ScanConfig(
        facets=7, rpm=9000, focal_m=0.1, pitch_x_m=1e-3, pitch_y_m=1e-3, nx=10, x_center_m=0
    )
    phases = {t % 20_000 for t in scan.sync_ticks(cfg, 40, CounterRng(1), 1)}
    assert len(phases) > 10, "an incoherent line rate must show many different phases"


def test_row_jitter_changes_line_starts_reproducibly():
    cfg = scan.ScanConfig(4, 6000, 0.1, 1e-3, 1e-3, 10, 0.0, row_jitter_s=1e-6)
    a = scan.sync_ticks(cfg, 10, CounterRng(2), 1)
    assert a == scan.sync_ticks(cfg, 10, CounterRng(2), 1)
    assert a != scan.sync_ticks(cfg, 10, CounterRng(3), 1)


# ---- afe ------------------------------------------------------------------------------------


def make_afe(**kw):
    base = dict(kind="log", polarity=1, v0=0.5, slope_v_per_dec=0.25, i_ref_a=1e-6)
    return afe.Afe(afe.AfeConfig(**{**base, **kw}), 1e-8)


def test_afe_dc():
    a = make_afe(pole_hz=1e6, aa_poles_hz=(2e6, 3e6))
    v, _ = a.process(np.full(2000, 1e-6))
    assert np.allclose(v, 0.5)


def test_afe_polarity_flips_the_slope():
    hi, lo = np.array([1e-5]), np.array([1e-7])
    up = make_afe(polarity=1)
    down = make_afe(polarity=-1)
    assert up.static(hi)[0] > up.static(lo)[0]
    assert down.static(hi)[0] < down.static(lo)[0]
    assert up.static(hi)[0] - 0.5 == pytest.approx(0.25)


def test_afe_log_floor_prevents_minus_infinity():
    a = make_afe(i_floor_a=1e-12)
    assert np.isfinite(a.static(np.array([0.0, -1.0]))).all()


def test_afe_clip_flag_and_driver_map():
    a = make_afe(driver_gain=2.0, driver_offset_v=0.1, clip_max_v=1.0)
    v, clipped = a.process(np.array([1e-7, 1e-4]))
    assert clipped.tolist() == [False, True] and v[1] == 1.0


def test_afe_state_survives_a_split_run_exactly():
    x = 1e-6 * (1 + np.random.default_rng(3).random(600))
    a = make_afe(pole_hz=1e6, aa_poles_hz=(2e6,))
    whole, _ = a.process(x)
    b = make_afe(pole_hz=1e6, aa_poles_hz=(2e6,))
    v1, _ = b.process(x[:200])
    c = make_afe(pole_hz=1e6, aa_poles_hz=(2e6,))
    c.set_state(b.state())
    v2, _ = c.process(x[200:])
    assert np.array_equal(whole, np.concatenate([v1, v2]))


def test_afe_step_convergence_is_better_than_the_zero_order_hold():
    fc, t_end = 1e6, 3e-6
    tau = 1 / (2 * math.pi * fc)

    def errors(dt):
        n = int(round(t_end / dt)) + 1
        t = np.arange(n) * dt
        x = 1e-3 * t  # a ramp starting at zero
        y = FirstOrder(fc, dt).process(x)
        # zero-order-hold version for comparison
        a = math.exp(-2 * math.pi * fc * dt)
        z = np.zeros(n)
        for i in range(1, n):
            z[i] = a * z[i - 1] + (1 - a) * x[i]
        exact = 1e-3 * (t - tau * (1 - np.exp(-t / tau)))
        return abs(y[-1] - exact[-1]), abs(z[-1] - exact[-1])

    foh = [errors(dt)[0] for dt in (2e-8, 1e-8)]
    zoh = [errors(dt)[1] for dt in (2e-8, 1e-8)]
    assert foh[1] < foh[0] * 0.5 + 1e-18 and foh[1] < zoh[1] / 5


def test_current_dependent_pole_is_slower_at_low_current():
    kw = dict(kind="log", polarity=1, v0=0, i_ref_a=1e-6, pole_hz=3e6)
    var = afe.Afe(afe.AfeConfig(**kw, current_dependent=True, knee_a=1e-8, floor_hz=2e4), 1e-8)
    fixed = afe.Afe(afe.AfeConfig(**kw), 1e-8)
    x = np.concatenate([np.full(50, 1e-10), np.full(200, 1e-9)])
    v_var, _ = var.process(x)
    v_fix, _ = fixed.process(x)
    rise_var, rise_fix = v_var[100] - v_var[49], v_fix[100] - v_fix[49]
    assert 0 < rise_var < rise_fix


# ---- adc ------------------------------------------------------------------------------------


def test_adc_quantisation_edges():
    c = adc.AdcConfig(16, 0.0, 2.5)
    codes, sat = adc.quantize(c, np.array([0.0, c.lsb * 0.99, c.lsb * 1.01, 1.25]))
    assert codes.tolist() == [0, 0, 1, 32768] and not sat.any()


def test_adc_saturation_is_its_own_flag():
    c = adc.AdcConfig(16, 0.0, 2.5)
    codes, sat = adc.quantize(c, np.array([-0.1, 2.5, 2.4999, 0.0]))
    assert sat.tolist() == [True, True, False, False]
    assert codes[0] == 0 and codes[3] == 0  # same code, different flag


def test_jitter_moves_the_sample_along_the_slope():
    v = adc.sample_with_jitter(np.array([1.0]), np.array([0.9]), 1e-8, np.array([2e-9]))
    assert v[0] == pytest.approx(1.02)


def test_step_wedge_scene_bands():
    s = step_wedge([0.0, 1.0], 0.1, 0.1, n=8)
    assert s.lookup(np.array([0.02]), np.array([0.05]))[0] == pytest.approx(1.0)
    assert s.lookup(np.array([0.08]), np.array([0.05]))[0] == pytest.approx(0.1)
