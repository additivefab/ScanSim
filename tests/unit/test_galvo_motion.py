"""Sinusoidal galvanometer trajectory candidate (N3, spec 13.5; default off)."""

import math
from dataclasses import replace

import numpy as np
import pytest

from zlscan.config.presets import demo_config
from zlscan.engine.chain import Chain
from zlscan.engine.physical_run import line_steps
from zlscan.engine.rng import CounterRng
from zlscan.truth import scan

RNG = CounterRng(1)
THETA = 1.0


def galvo(**over):
    base = demo_config(1).scan
    return replace(base, facets=1, rpm=6000.0, galvo_max_phase_rad=THETA, **over)


def line_x(cfg, line, n=401):
    ta = cfg.window_start_s()
    t = line / cfg.f_line + ta + np.linspace(0, cfg.active_s, n, endpoint=False)
    return scan.geometry(cfg, t, RNG, 1)[1], t


def test_window_ends_and_centre_match_the_polygon_window():
    cfg = galvo()
    x, _ = line_x(cfg, 0, n=400)
    lo, hi = cfg.x_center_m - cfg.width_m / 2, cfg.x_center_m + cfg.width_m / 2
    assert x[0] == pytest.approx(lo)
    assert x[len(x) // 2] == pytest.approx(cfg.x_center_m, abs=1e-9)
    assert lo - 1e-12 <= x.min() and x.max() <= hi + 1e-12
    assert np.all(np.diff(x) > 0)


def test_speed_is_not_constant_and_follows_cos_of_the_phase():
    cfg = galvo()
    x, t = line_x(cfg, 0, n=4001)
    v = np.gradient(x, t)
    ratio = v[10] / v[len(v) // 2]  # near the edge versus the centre
    assert ratio == pytest.approx(math.cos(THETA * 0.995), rel=2e-2)
    assert v.max() > 1.3 * v.min()


def test_bidirectional_odd_lines_run_backwards_over_the_same_window():
    cfg = galvo(galvo_bidirectional=True)
    even, _ = line_x(cfg, 0)
    odd, _ = line_x(cfg, 1)
    assert np.all(np.diff(odd) < 0)
    assert odd[0] == pytest.approx(even.max() + (even[1] - even[0]) * 0, abs=2e-3 * cfg.width_m)
    uni = galvo()
    assert np.all(np.diff(line_x(uni, 1)[0]) > 0)


def test_off_keeps_identity_validates_and_polygon_is_linear():
    cfg = demo_config(1)
    assert "galvo_max_phase_rad" not in cfg.hash_payload()["scan"]
    assert "galvo_bidirectional" not in cfg.hash_payload()["scan"]
    for bad in (0.0, -0.5, 1.7, float("nan")):
        with pytest.raises(ValueError):
            replace(cfg.scan, galvo_max_phase_rad=bad)
    with pytest.raises(ValueError):
        replace(cfg.scan, galvo_bidirectional=True)
    x, _ = line_x(cfg.scan, 0)
    assert np.allclose(np.diff(x), np.diff(x)[0])


def test_chain_with_galvo_is_chunk_independent_and_differs_from_polygon():
    cfg = demo_config(3)
    cfg.scan = galvo(galvo_bidirectional=True)
    total = line_steps(cfg) * 4
    whole = Chain(cfg).run_steps(total, total)
    assert np.array_equal(whole.codes, Chain(cfg).run_steps(total, 701).codes)
    poly = demo_config(3)
    poly.scan = replace(poly.scan, facets=1, rpm=6000.0)
    assert not np.array_equal(whole.codes, Chain(poly).run_steps(total, total).codes)
