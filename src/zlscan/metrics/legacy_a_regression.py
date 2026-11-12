"""Package A deterministic compatibility evaluator, isolated from reconstruction."""

from __future__ import annotations

import math
import runpy
from dataclasses import dataclass, replace

import numpy as np

from zlscan.config.presets import from_legacy_a
from zlscan.engine.chain import Chain
from zlscan.evidence.legacy_runner import LEGACY_ROOT
from zlscan.truth.adc import quantize
from zlscan.truth.detector import C, E, H
from zlscan.truth.scene import Scene


@dataclass(frozen=True)
class IdealAResult:
    codes: int
    differing_codes: int
    rmse_v5: float
    rmse_legacy: float


def run_ideal_a_regression() -> IdealAResult:
    """Compare the complete noiseless frame with an independent frozen-package run."""
    package = LEGACY_ROOT / "package_a"
    legacy = runpy.run_path(str(package / "src/scanner_sim.py"))
    config = legacy["load_config"](package / "config/ideal.json")
    reference = legacy["run"](config)
    cfg, _ = from_legacy_a(config, config["seed"])
    nx, ny = config["nx"], config["ny"]
    spp = config["samples_per_pixel"]
    width, height = cfg.scan.width_m, ny * cfg.scan.pitch_y_m
    velocity = cfg.scan.pitch_x_m * cfg.fs_hz / spp
    cfg.scan = replace(
        cfg.scan,
        focal_m=velocity / (2 * cfg.scan.omega),
        continuous_feed=False,
        y0_m=cfg.scan.pitch_y_m / 2,
        x_center_m=width / 2,
    )
    cfg.source = replace(cfg.source, gated=False)
    # Package A sample_scene uses map_coordinates(mode="nearest") even in its guard.
    # Preserve that explicit legacy scene boundary without clamping the physical trajectory.
    cfg.scenes = {
        "main": Scene(
            reference["blurred_transmission"], width, height, legacy_nearest_boundary=True
        )
    }
    # Doubling the grid places all legacy midpoints and ADC edges on fine ticks.
    cfg.os = 2 * config["analog_oversample"]
    active_samples = nx * spp
    line_steps = round(cfg.scan.line_period_s * cfg.fs_hz * cfg.os)
    gate_start = round(cfg.scan.window_start_s() * cfg.fs_hz - 0.5)
    pixel_start = math.ceil(cfg.scan.window_start_s() * cfg.fs_hz - 0.5)
    # Package A's raw gate uses round while its pixel-grid reference uses physical sample centres.
    extra = pixel_start - gate_start
    midpoint = np.arange(active_samples + extra) * cfg.os + cfg.os // 2
    chain = Chain(cfg)
    chain.probe_names = {"v_adc"}
    codes = np.empty((ny, active_samples + extra), dtype=np.uint16)
    for line in range(ny):
        # This specific ideal configuration has no analogue memory, so guards are immaterial.
        chain.step = line * line_steps + gate_start * cfg.os
        volts = chain.advance((active_samples + extra) * cfg.os).probes["v_adc"]
        sampled = volts[midpoint]
        if config["analog_oversample"] % 2 == 0:
            sampled = (volts[midpoint - 1] + volts[midpoint + 1]) / 2
        codes[line], _ = quantize(cfg.adc, sampled)

    def inverse(values: np.ndarray) -> np.ndarray:
        volts = cfg.adc.vmin + (values.astype(float) + 0.5) * cfg.adc.lsb
        return cfg.afe.i_ref_a * 10 ** ((volts - cfg.afe.v0) / cfg.afe.slope_v_per_dec)

    energy_j = H * C / (cfg.detector.wavelength_nm * 1e-9)
    power_w = cfg.source.power_w * cfg.collect.eta_pre * cfg.collect.eta_collect
    dark_a = cfg.detector.dark_current_a
    white_a = dark_a + power_w / energy_j * cfg.detector.qe * E * cfg.detector.gain
    reference_v = chain.afe.static(np.array([dark_a, white_a]))
    reference_code, _ = quantize(cfg.adc, reference_v)
    dark, white = inverse(reference_code)
    trans = (inverse(codes[:, extra:]).reshape(ny, nx, spp).mean(axis=-1) - dark) / (white - dark)
    if np.any(trans <= 0):
        raise ValueError("nonpositive intensity in the ideal compatibility fixture")
    density = -np.log10(trans)
    errors = (density - reference["optical_reference_density"])[2:-2, 2:-2]
    return IdealAResult(
        ny * active_samples,
        int(np.count_nonzero(codes[:, :active_samples] != reference["active_codes"])),
        float(np.sqrt(np.mean(errors**2))),
        reference["metrics"]["density_rmse_valid_interior"],
    )
