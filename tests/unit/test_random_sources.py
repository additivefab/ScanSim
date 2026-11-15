"""Common (scanner, laser) and independent (detector, ADC) random sources (N2)."""

from dataclasses import replace

import numpy as np
import pytest

from zlscan.config.presets import demo_config
from zlscan.engine.chain import Chain
from zlscan.engine.physical_run import line_steps
from zlscan.engine.stream_physical import stream_physical


def noisy(seed, common_seed=None):
    cfg = demo_config(seed)
    cfg.scan = replace(cfg.scan, row_jitter_s=2e-6)
    cfg.source = replace(cfg.source, line_sigma=0.02)
    cfg.common_seed = common_seed
    return cfg


def run(cfg, lines=6, probes=("x_m", "laser_power_w", "current_a")):
    chain = Chain(cfg)
    chain.probe_names = set(probes)
    return chain.run_steps(line_steps(cfg) * lines, 997)


def test_default_common_source_is_the_channel_seed_bitwise():
    implicit, explicit = run(noisy(5)), run(noisy(5, common_seed=5))
    assert np.array_equal(implicit.codes, explicit.codes)
    assert noisy(5).hash() == demo_hash_with_noise(5)


def demo_hash_with_noise(seed):
    cfg = noisy(seed)
    assert cfg.common_seed is None
    # A configuration that does not use the option keeps its pre-option identity.
    assert "common_seed" not in cfg.hash_payload()
    return cfg.hash()


def test_channels_share_scanner_and_laser_but_not_detector_noise():
    a, b = run(noisy(11, common_seed=3)), run(noisy(12, common_seed=3))
    assert np.array_equal(a.probes["x_m"], b.probes["x_m"])
    assert np.array_equal(a.probes["laser_power_w"], b.probes["laser_power_w"])
    assert not np.array_equal(a.probes["current_a"], b.probes["current_a"])
    assert np.array_equal(a.codes, run(noisy(11, common_seed=3)).codes)  # reproducible


def test_different_common_seed_changes_scanner_and_laser():
    a, b = run(noisy(11, common_seed=3)), run(noisy(11, common_seed=4))
    assert not np.array_equal(a.probes["x_m"], b.probes["x_m"])
    assert not np.array_equal(a.probes["laser_power_w"], b.probes["laser_power_w"])


def test_detector_noise_is_statistically_independent_between_channels():
    def current(seed, shot_noise=True):
        cfg = demo_config(seed)
        cfg.detector = replace(cfg.detector, shot_noise=shot_noise)
        cfg.common_seed = 9
        out = run(cfg, lines=12, probes=("current_a", "scan_active"))
        return out.probes["current_a"][out.probes["scan_active"]]

    mean = current(21, shot_noise=False)  # deterministic part, identical for both channels
    a, b = current(21) - mean, current(22) - mean
    assert a.std() > 0 and b.std() > 0
    r = np.corrcoef(a, b)[0, 1]
    assert abs(r) < 5 / np.sqrt(len(a))


def test_dual_stream_requires_a_common_scanner_source_not_a_common_seed(tmp_path):
    ok = stream_physical(tmp_path / "ok", noisy(1, common_seed=7), noisy(2, common_seed=7), 2)
    assert ok["integrity"] == "complete"
    with pytest.raises(ValueError, match="common scan"):
        stream_physical(tmp_path / "bad", noisy(1, common_seed=7), noisy(2, common_seed=8), 2)
    with pytest.raises(ValueError, match="common scan"):
        stream_physical(tmp_path / "bad2", noisy(1), noisy(2), 2)
