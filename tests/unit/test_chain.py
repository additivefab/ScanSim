"""Forward chain: chunk invariance, checkpoint, probe non-interference, windows, statistics."""

import numpy as np
import pytest

from zlscan.config.presets import clear_scene, demo_config, with_timeline
from zlscan.engine.chain import Chain, Phase
from zlscan.truth.scene import Scene

LINE_STEPS = 10_000  # 2.5 ms * 1 MHz * 4


def run(cfg, steps, chunk):
    return Chain(cfg).run_steps(steps, chunk)


def test_the_line_period_matches_the_declared_geometry():
    cfg = demo_config()
    assert cfg.scan.f_line == 400
    assert round(cfg.scan.line_period_s * cfg.fs_hz * cfg.os) == LINE_STEPS


def test_codes_are_identical_for_any_chunking():
    steps = 2 * LINE_STEPS + 123
    a = run(demo_config(), steps, steps)
    b = run(demo_config(), steps, 997)
    c = run(demo_config(), steps, LINE_STEPS // 3)
    assert np.array_equal(a.codes, b.codes) and np.array_equal(a.codes, c.codes)
    assert np.array_equal(a.saturated, b.saturated)


def test_a_checkpoint_restores_a_run_that_matches_the_uninterrupted_one():
    steps = 3 * LINE_STEPS
    whole = run(demo_config(), steps, 2000)
    ch = Chain(demo_config())
    first = ch.run_steps(LINE_STEPS + 777, 2000)
    blob = ch.checkpoint()
    resumed = Chain(demo_config())
    resumed.restore(blob)
    rest = resumed.run_steps(steps - ch.step, 1500)
    assert np.array_equal(np.concatenate([first.codes, rest.codes]), whole.codes)


def test_a_checkpoint_from_another_configuration_is_refused():
    ch = Chain(demo_config())
    ch.advance(100)
    blob = ch.checkpoint()
    other = demo_config()
    other.detector = type(other.detector)(**{**other.detector.__dict__, "gain": 999.0})
    with pytest.raises(ValueError, match="different configuration"):
        Chain(other).restore(blob)


def test_probes_change_nothing_in_the_codes():
    steps = LINE_STEPS + 500
    base = run(demo_config(), steps, 1000)
    ch = Chain(demo_config())
    ch.probe_names = {"power_w", "current_a", "v_adc", "t_eff", "x_m", "line"}
    seen = []
    ch.observers.append(lambda name, s0, arr: seen.append((name, s0)))
    probed = ch.run_steps(steps, 1000)
    assert np.array_equal(base.codes, probed.codes) and seen
    assert set(probed.probes) == ch.probe_names


def test_the_same_seed_reproduces_and_another_seed_differs():
    a = run(demo_config(seed=3), LINE_STEPS, 4000).codes
    assert np.array_equal(a, run(demo_config(seed=3), LINE_STEPS, 4000).codes)
    assert not np.array_equal(a, run(demo_config(seed=4), LINE_STEPS, 4000).codes)


def test_without_shot_noise_the_chain_is_deterministic_and_density_orders_the_codes():
    cfg = demo_config()
    cfg.detector = type(cfg.detector)(**{**cfg.detector.__dict__, "shot_noise": False})
    out = run(cfg, LINE_STEPS, 3000)
    ch = Chain(cfg)
    ch.probe_names = {"x_m", "line"}
    probe = ch.run_steps(LINE_STEPS, 3000).probes
    x = probe["x_m"][::4]
    codes = out.codes.astype(float)
    inside = (x > 0.0405) & (x < 0.0795) & (np.arange(len(x)) < LINE_STEPS // 4)
    bands = np.floor((x[inside]) / 0.02).astype(int)
    means = [codes[inside][bands == b].mean() for b in sorted(set(bands))[1:-1]]
    assert means == sorted(means)  # polarity -1: denser film, higher voltage and code


def test_dc_in_gives_dc_out_through_the_front_end():
    cfg = demo_config()
    cfg.detector = type(cfg.detector)(**{**cfg.detector.__dict__, "shot_noise": False})
    cfg.scenes = {"main": Scene.from_density(np.full((8, 8), 1.0), 0.08, 0.2, off_film_T=0.1)}
    cfg.source = type(cfg.source)(power_w=1e-3, gated=False)
    out = run(cfg, 4000, 4000)
    assert out.codes.max() - out.codes.min() <= 1


def test_out_of_film_illumination_changes_the_front_end_state_even_if_the_codes_are_discarded():
    """Strong light just before a dense region is still decaying when the dense region starts."""
    dense = Scene.from_density(np.full((8, 8), 3.0), 0.08, 0.2, off_film_T=1.0)
    cfg_a = demo_config()
    cfg_a.scenes = {"main": dense}
    cfg_a.scan = type(cfg_a.scan)(**{**cfg_a.scan.__dict__, "x_center_m": 0.04})
    cfg_a.source = type(cfg_a.source)(power_w=1e-3, gated=False)  # ungated: light also outside
    cfg_b = demo_config()
    cfg_b.scenes = {"main": dense}
    cfg_b.source = type(cfg_b.source)(power_w=1e-3, gated=True)  # dark outside the window
    for c in (cfg_a, cfg_b):
        c.detector = type(c.detector)(**{**c.detector.__dict__, "shot_noise": False})
    a = Chain(cfg_a)
    b = Chain(cfg_b)
    ta = a.cfg.scan.window_start_s()
    n = int(ta * cfg_a.fs_hz * cfg_a.os) + 40  # just past the window start
    ca, cb = a.advance(n), b.advance(n)
    start = int(ta * cfg_a.fs_hz)
    assert not np.array_equal(ca.codes[start : start + 10], cb.codes[start : start + 10])


def test_the_experiment_timeline_darkens_then_whitens_with_continuous_state():
    cfg = with_timeline(
        demo_config(),
        [
            Phase("dark", 0, 1, "main", laser_on=False),
            Phase("white", 1, 1, "white"),
            Phase("sample", 2, 1, "main"),
        ],  # fmt: skip
        {"white": clear_scene()},
    )
    cfg.detector = type(cfg.detector)(**{**cfg.detector.__dict__, "shot_noise": False})
    ch = Chain(cfg)
    out = ch.run_steps(3 * LINE_STEPS, 5000)
    per_line = np.array_split(out.codes, 3)
    # dark line: only dark current -> lowest voltage with polarity -1 means code at the dark level
    assert per_line[0].max() > per_line[1].min()
    assert per_line[1].min() < per_line[2].max()


def test_the_pmt_mean_and_variance_follow_poisson_gamma_theory():
    from zlscan.engine.rng import CounterRng
    from zlscan.truth.detector import DetectorConfig, E, anode_current, mean_counts

    cfg = DetectorConfig(qe=0.15, gain=1000.0, excess_noise_factor=1.3, dark_current_a=0.0,
                         wavelength_nm=635.0)  # fmt: skip
    dt = 1e-7
    p = np.full(200_000, 1e-9)
    mu = mean_counts(cfg, p, dt)[0]
    cur, _ = anode_current(cfg, p, dt, CounterRng(1), 1, 0)
    q = cur * dt / E
    assert q.mean() == pytest.approx(mu * 1000, rel=0.02)
    assert q.var() == pytest.approx(1.3 * mu * 1000**2, rel=0.05)


def test_poisson_counts_are_not_gaussian_at_low_mean():
    from zlscan.engine.rng import CounterRng
    from zlscan.truth.detector import DetectorConfig, anode_current

    cfg = DetectorConfig(qe=1.0, gain=1.0, excess_noise_factor=1.0, dark_current_a=0.0,
                         wavelength_nm=500.0)  # fmt: skip
    dt = 1e-6
    p = np.full(100_000, 0.3 * 6.62607015e-34 * 299792458 / 500e-9 / dt)  # mu = 0.3
    cur, _ = anode_current(cfg, p, dt, CounterRng(2), 1, 0)
    zeros = np.mean(cur == 0)
    assert zeros == pytest.approx(np.exp(-0.3), abs=0.01)


def test_the_whole_chain_converges_as_the_fine_step_shrinks():
    """Noise off, same ADC sample instants: the code difference to a fine reference shrinks."""

    def codes(os_):
        cfg = demo_config()
        cfg.os = os_
        cfg.psf = type(cfg.psf)(
            8e-3, 8e-3, nodes_x=17
        )  # smooth input; a sharp edge converges slower
        cfg.detector = type(cfg.detector)(**{**cfg.detector.__dict__, "shot_noise": False})
        # ungated: a hard on/off gate edge is a discontinuity whose position depends on the grid
        cfg.source = type(cfg.source)(power_w=1e-3, gated=False)
        n_samples = 2400  # stay inside the first line: the flyback is a true discontinuity
        return Chain(cfg).run_steps(n_samples * os_, 4000).codes.astype(float)

    ref = codes(32)
    err = {os_: np.abs(codes(os_)[50:] - ref[50:]).max() for os_ in (2, 4, 8, 16)}
    assert err[16] <= err[8] <= err[4] <= err[2]
    assert err[16] < err[2] / 4  # roughly first order here: a kink in the beam path limits it
