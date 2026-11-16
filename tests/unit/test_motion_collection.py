"""Feed speed ripple, feed encoder observation and a collection position table (N3, default off)."""

import math
from dataclasses import replace

import numpy as np
import pytest

from zlscan.config.presets import demo_config
from zlscan.engine.chain import Chain
from zlscan.engine.physical_run import line_steps
from zlscan.engine.rng import CounterRng
from zlscan.truth import collect, scan
from zlscan.truth.scene import Scene

RNG = CounterRng(1)


def geom(cfg, t):
    return scan.geometry(cfg, np.asarray(t, float), RNG, 1)


def test_feed_ripple_integrates_the_speed_modulation():
    base = demo_config(1).scan
    ripple = replace(base, feed_ripple=0.1, feed_ripple_hz=50.0)
    t = np.linspace(0, 0.05, 11)
    y0 = geom(base, t)[2]
    y1 = geom(ripple, t)[2]
    w = 2 * math.pi * 50.0
    expected = base.v_y * (t + 0.1 * (1 - np.cos(w * t)) / w)
    assert y0 == pytest.approx(base.v_y * t)
    assert y1 == pytest.approx(expected)
    assert y1[0] == 0.0 and not np.allclose(y1, y0)


def test_ripple_off_keeps_identity_and_output():
    cfg = demo_config(1)
    assert "feed_ripple" not in cfg.hash_payload()["scan"]
    with_ripple = replace(cfg, scan=replace(cfg.scan, feed_ripple=0.05, feed_ripple_hz=30.0))
    assert with_ripple.hash() != cfg.hash()


def test_ripple_needs_a_frequency_and_a_sane_amplitude():
    base = demo_config(1).scan
    with pytest.raises(ValueError):
        replace(base, feed_ripple=0.1)
    with pytest.raises(ValueError):
        replace(base, feed_ripple=1.0, feed_ripple_hz=10.0)
    with pytest.raises(ValueError):
        replace(base, encoder_pitch_m=0.0)


def test_encoder_observes_true_position_quantised_not_the_nominal_one():
    sc = replace(demo_config(1).scan, feed_ripple=0.2, feed_ripple_hz=40.0, encoder_pitch_m=1e-5)
    k = np.arange(40)
    t = k / sc.f_line
    counts = scan.encoder_counts(sc, t)
    truth = geom(sc, t)[2]
    assert counts == pytest.approx(np.floor(truth / 1e-5 + 1e-9))
    nominal = np.floor(sc.v_y * t / 1e-5 + 1e-9)
    assert np.abs(counts - nominal).max() > 0
    assert scan.encoder_counts(replace(sc, encoder_pitch_m=None), t) is None


def test_collection_table_gives_the_clear_field_line_profile():
    cfg = demo_config(2)
    cfg.detector = replace(cfg.detector, shot_noise=False, dark_current_a=0.0)
    cfg.scenes = {"main": Scene.from_density(np.zeros((4, 4)), 0.08, 0.2)}
    cfg.collect = replace(cfg.collect, profile_u=(0.0, 0.5, 1.0), profile_rel=(0.8, 1.0, 0.9))
    chain = Chain(cfg)
    chain.probe_names = {"power_w", "x_m", "scan_active"}
    out = chain.run_steps(line_steps(cfg) * 2, 1000)
    live = out.probes["scan_active"]
    p, x = out.probes["power_w"][live], out.probes["x_m"][live]
    u = np.clip((x - (cfg.scan.x_center_m - cfg.scan.width_m / 2)) / cfg.scan.width_m, 0, 1)
    eff = (
        cfg.collect.eta_pre
        * cfg.collect.eta_collect
        * np.interp(u, (0.0, 0.5, 1.0), (0.8, 1.0, 0.9))
    )
    assert p == pytest.approx(cfg.source.power_w * eff, rel=1e-9)


def test_collection_table_validation_and_identity():
    c = collect.CollectConfig(0.4, 0.02)
    with pytest.raises(ValueError):
        replace(c, profile_u=(0.0, 1.0))
    with pytest.raises(ValueError):
        replace(c, profile_u=(0.0, 0.0), profile_rel=(1.0, 1.0))
    with pytest.raises(ValueError):
        replace(c, profile_u=(0.0, 1.0), profile_rel=(1.0, -0.1))
    cfg = demo_config(1)
    assert "profile_u" not in cfg.hash_payload()["collect"]
