"""PMT anode compression and gain droop/recovery candidate (N2, spec 13.2; default off)."""

import math
from dataclasses import replace

import numpy as np
import pytest

from zlscan.config.presets import demo_config
from zlscan.engine.chain import Chain
from zlscan.engine.physical_run import line_steps
from zlscan.truth.detector import DetectorConfig, Saturation

DT = 1e-6


def test_static_compression_is_the_declared_soft_knee():
    s = Saturation(DetectorConfig(0.2, 1e3, 1.0, 0.0, 635.0, sat_current_a=1e-4), DT)
    i = np.array([0.0, 1e-6, 1e-4, 1e-2])
    assert s.process(i) == pytest.approx(i / (1 + i / 1e-4))


def test_droop_steady_state_and_exponential_recovery():
    cfg = DetectorConfig(0.2, 1e3, 1.0, 0.0, 635.0, droop_per_a=2e3, droop_tau_s=1e-3)
    s = Saturation(cfg, DT)
    bright = s.process(np.full(20_000, 1e-4))  # 20 tau
    assert bright[0] == pytest.approx(1e-4)  # no droop before any charge has flowed
    assert bright[-1] == pytest.approx(1e-4 * (1 - 2e3 * 1e-4), rel=1e-6)
    dim = s.process(np.full(1001, 1e-9))
    droop = 1 - dim / 1e-9
    # d(t) relaxes from kappa*I_bright to kappa*I_dim with the declared time constant.
    target = 2e3 * 1e-9 + (0.2 - 2e3 * 1e-9) * math.exp(-1.0)
    assert droop[0] == pytest.approx(0.2, rel=1e-6)
    assert droop[1000] == pytest.approx(target, rel=1e-6)


def test_gain_never_goes_negative_and_state_round_trips():
    cfg = DetectorConfig(0.2, 1e3, 1.0, 0.0, 635.0, droop_per_a=1e5, droop_tau_s=1e-4)
    a, b = Saturation(cfg, DT), Saturation(cfg, DT)
    x = np.full(5000, 1e-4)  # kappa * I = 10: the candidate clips the gain at zero
    whole = a.process(x)
    assert whole.min() == 0.0 and np.all(whole >= 0)
    first = b.process(x[:1234])
    c = Saturation(cfg, DT)
    c.set_state(b.state())
    assert np.array_equal(np.concatenate([first, c.process(x[1234:])]), whole)


@pytest.mark.parametrize(
    "bad",
    [
        dict(sat_current_a=0.0),
        dict(droop_per_a=1e3),
        dict(droop_tau_s=1e-3),
        dict(droop_per_a=-1.0, droop_tau_s=1e-3),
        dict(droop_per_a=1e3, droop_tau_s=0.0),
        dict(sat_current_a=float("nan")),
    ],
)
def test_invalid_saturation_parameters_are_rejected(bad):
    with pytest.raises(ValueError):
        DetectorConfig(0.2, 1e3, 1.0, 0.0, 635.0, **bad)


def saturating(seed=3):
    cfg = demo_config(seed)
    cfg.detector = replace(cfg.detector, sat_current_a=2e-4, droop_per_a=1e3, droop_tau_s=2e-4)
    return cfg


def test_option_off_keeps_configuration_identity_and_has_no_state():
    cfg = demo_config(3)
    assert Chain(cfg).saturation is None
    assert "sat_current_a" not in cfg.hash_payload()["detector"]
    assert saturating().hash() != cfg.hash()


def test_chain_with_saturation_is_chunk_and_checkpoint_independent():
    cfg = saturating()
    total = line_steps(cfg) * 5
    whole = Chain(cfg).run_steps(total, total)
    chunked = Chain(cfg).run_steps(total, 613)
    assert np.array_equal(whole.codes, chunked.codes)
    first = Chain(cfg)
    head = first.run_steps(1700, 500)
    resumed = Chain(cfg)
    resumed.restore(first.checkpoint())
    tail = resumed.run_steps(total - 1700, 911)
    assert np.array_equal(np.concatenate([head.codes, tail.codes]), whole.codes)
    assert not np.array_equal(whole.codes, Chain(demo_config(3)).run_steps(total, total).codes)


def test_bright_off_film_history_lowers_the_following_signal():
    """The candidate's purpose: light before the film edge changes the reading after it."""
    cfg = saturating()
    cfg.detector = replace(cfg.detector, shot_noise=False)
    ideal = demo_config(3)
    ideal.detector = replace(ideal.detector, shot_noise=False)
    total = line_steps(cfg) * 3

    def current(c):
        chain = Chain(c)
        chain.probe_names = {"current_a"}
        return chain.run_steps(total, 1000).probes["current_a"]

    a, b = current(cfg), current(ideal)
    lit = b > 0
    assert np.all(a[lit] < b[lit])


def test_applicability_names_the_candidate_instead_of_claiming_physics():
    from zlscan.engine.validity import physical_applicability

    on = physical_applicability(saturating())["limitations"]
    off = physical_applicability(demo_config(3))["limitations"]
    assert any("compression/droop candidate" in text for text in on)
    assert any("warning only" in text for text in off)
    assert not any("warning only" in text for text in on)
