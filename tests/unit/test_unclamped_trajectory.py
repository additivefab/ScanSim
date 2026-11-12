from dataclasses import replace

import numpy as np
import pytest

from zlscan.config.presets import demo_config
from zlscan.engine.chain import Chain
from zlscan.engine.rng import CounterRng
from zlscan.truth.psf import PsfConfig
from zlscan.truth.scan import geometry
from zlscan.truth.scene import Scene


def test_physical_coordinate_continues_before_and_after_retained_window():
    cfg = demo_config().scan
    start = cfg.window_start_s()
    _, x, _, _, inside = geometry(
        cfg, np.array([start - 1e-4, start + cfg.active_s + 1e-4]), CounterRng(1), 1
    )
    np.testing.assert_allclose(x, [-cfg.v_x * 1e-4, cfg.width_m + cfg.v_x * 1e-4])
    assert not inside.any()
    scene = Scene.from_density(np.full((4, 4), 3.0), cfg.width_m, 1, off_film_T=1)
    np.testing.assert_array_equal(scene.lookup(x, np.zeros(2)), [1, 1])


def test_point_spot_off_film_light_changes_later_retained_samples():
    base = demo_config()
    base.detector = replace(base.detector, shot_noise=False)
    base.source = replace(base.source, gated=False)
    base.psf = PsfConfig(0, 0)
    base.afe = replace(base.afe, pole_hz=5000, aa_poles_hz=())

    def run(off_transmission):
        scene = Scene.from_density(
            np.full((4, 4), 3.0), base.scan.width_m, 1, off_film_T=off_transmission
        )
        cfg = replace(base, scenes={"main": scene})
        chain = Chain(cfg)
        chain.probe_names = {"t_eff", "x_m"}
        samples = round(cfg.scan.window_start_s() * cfg.fs_hz) + 40
        return chain.run_steps(samples * cfg.os, 617)

    transparent, opaque = run(1), run(1e-3)
    pre = np.flatnonzero(transparent.probes["x_m"] < -1e-3)
    assert len(pre) > 0
    np.testing.assert_array_equal(transparent.probes["t_eff"][pre], np.ones(len(pre)))
    first = int(np.ceil(base.scan.window_start_s() * base.fs_hz))
    assert (
        np.max(
            abs(
                transparent.codes[first : first + 10].astype(float)
                - opaque.codes[first : first + 10]
            )
        )
        > 100
    )


def test_explicit_legacy_clamp_is_labeled_and_does_not_become_default():
    base = demo_config()
    assert not base.scan.legacy_clamped_coordinates
    cfg = replace(base.scan, legacy_clamped_coordinates=True)
    _, x, _, _, _ = geometry(cfg, np.array([cfg.window_start_s() - 1e-4]), CounterRng(1), 1)
    assert x[0] == pytest.approx(0)


def test_scene_boundary_policy_is_explicit_and_part_of_configuration_identity():
    base = demo_config()
    normal = base.scenes["main"]
    legacy = replace(normal, legacy_nearest_boundary=True)
    x, y = np.array([-0.01]), np.array([0.01])
    assert normal.lookup(x, y)[0] == 1
    assert legacy.lookup(x, y)[0] < 1
    assert base.hash() != replace(base, scenes={"main": legacy}).hash()
