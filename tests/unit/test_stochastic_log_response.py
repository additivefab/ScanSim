from dataclasses import replace

import numpy as np
import pytest
from scipy.stats import poisson

from zlscan.config.presets import demo_config
from zlscan.engine.chain import Chain
from zlscan.engine.rng import CounterRng
from zlscan.engine.validity import physical_applicability
from zlscan.truth.detector import C, DetectorConfig, DetectorResponse, E, H, anode_current
from zlscan.truth.psf import PsfConfig
from zlscan.truth.scene import uniform


def constant_rate_config(os, *, rate=1e8, response_hz=20_000, seed=11):
    cfg = demo_config(seed)
    cfg.os = os
    photon = H * C / (cfg.detector.wavelength_nm * 1e-9)
    power = rate * photon / (cfg.detector.qe * cfg.collect.eta_pre * cfg.collect.eta_collect)
    cfg.source = replace(cfg.source, power_w=power, gated=False)
    cfg.detector = replace(
        cfg.detector,
        excess_noise_factor=1,
        dark_current_a=0,
        pole_hz=response_hz,
        anode_limit_a=None,
    )
    cfg.afe = replace(cfg.afe, polarity=1, pole_hz=30_000, aa_poles_hz=(50_000,))
    cfg.scenes = {"main": uniform(0, cfg.scan.width_m, 1)}
    cfg.psf = PsfConfig(0, 0)
    return cfg


def test_exact_poisson_sum_reproduces_the_unfiltered_log_grid_bias():
    expected_mv = [-2.25, -4.69, -11.73, -56.46]
    for os, expected in zip((4, 8, 16, 32), expected_mv, strict=True):
        dt = 1 / (1e6 * os)
        count = np.arange(300)
        probabilities = poisson.pmf(count, 1e8 * dt)
        volts = 0.25 * np.log10(np.maximum(count * E * 1000 / dt, 1e-12))
        bias = np.sum(probabilities * volts) - 0.25 * np.log10(1e8 * E * 1000)
        assert bias * 1000 == pytest.approx(expected, abs=0.01)


def test_fixed_response_has_analytic_dc_and_step_and_preserves_state():
    fc, dt = 20_000, 1e-7
    values = np.full(1000, 2e-8)
    whole = DetectorResponse(fc, dt).process(values)
    reference = values[0] * (1 - np.exp(-2 * np.pi * fc * dt * np.arange(1, 1001)))
    np.testing.assert_allclose(whole, reference, atol=1e-22)
    part = DetectorResponse(fc, dt)
    first = part.process(values[:317])
    resumed = DetectorResponse(fc, dt)
    resumed.set_state(part.state())
    np.testing.assert_array_equal(np.r_[first, resumed.process(values[317:])], whole)


def test_response_current_variance_matches_compound_poisson_theory():
    dt, fc, rate = 1e-7, 20_000, 2e7
    cfg = DetectorConfig(
        qe=1, gain=1000, excess_noise_factor=1.3, dark_current_a=0, wavelength_nm=635, pole_hz=fc
    )
    power = rate * H * C / (cfg.wavelength_nm * 1e-9)
    current, _ = anode_current(cfg, np.full(150_000, power), dt, CounterRng(17), 1, 0)
    output = DetectorResponse(fc, dt).process(current)[5000:]
    tau = 1 / (2 * np.pi * fc)
    expected_var = (
        cfg.excess_noise_factor * rate * (E * cfg.gain) ** 2 * np.tanh(dt / (2 * tau)) / dt
    )
    assert output.mean() == pytest.approx(rate * E * cfg.gain, rel=0.02)
    assert output.var() == pytest.approx(expected_var, rel=0.10)


def test_noisy_log_mean_and_band_limited_variance_are_grid_consistent():
    statistics = []
    for os in (4, 8, 16, 32):
        chain = Chain(constant_rate_config(os))
        chain.probe_names = {"v_adc"}
        values = chain.run_steps(8192 * os, 1537).probes["v_adc"][::os][512:]
        batches = np.array_split(values, 15)
        statistics.append(
            (
                values.mean(),
                values.var(),
                np.std([a.mean() for a in batches], ddof=1) / np.sqrt(len(batches)),
            )
        )
    reference_mean, reference_var, reference_se = statistics[-1]
    for mean, variance, se in statistics[:-1]:
        assert abs(mean - reference_mean) < 4 * (se + reference_se)
        assert variance == pytest.approx(reference_var, rel=0.25)


def test_chain_checkpoint_includes_pre_log_response_and_is_chunk_invariant():
    cfg = constant_rate_config(8, rate=1e6)
    whole = Chain(cfg).run_steps(12_000, 12_000)
    chain = Chain(cfg)
    first = chain.advance(3457)
    blob = chain.checkpoint()
    resumed = Chain(cfg)
    resumed.restore(blob)
    rest = resumed.run_steps(8543, 617)
    np.testing.assert_array_equal(np.r_[first.codes, rest.codes], whole.codes)


def test_missing_physical_input_response_is_never_capability_validation():
    cfg = demo_config()
    result = physical_applicability(cfg)
    assert result["stochastic_log_status"] == "grid_dependent_exploration_only"
    assert result["high_density_capability"] == "not_validated"
    modeled = physical_applicability(constant_rate_config(8))
    assert modeled["input_response_evidence"] == "assumed"
    assert modeled["high_density_capability"] == "not_validated"


def test_cli_and_offline_report_disclose_unfiltered_noisy_log_limit(tmp_path, capsys):
    import json

    from zlscan.cli import main

    output = tmp_path / "limited"
    assert main(["run", "--mode", "physical", "--lines", "1", "--out", str(output)]) == 0
    assert "依赖数值时间格" in capsys.readouterr().out
    manifest = json.loads((output / "run.json").read_text())
    assert manifest["physical_applicability"]["high_density_capability"] == "not_validated"
    assert "高密度能力未验证" in (output / "viewer.html").read_text()
