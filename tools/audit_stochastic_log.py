"""Reproduce the charge-bin/log bias and inspect fixed-response grid statistics (ASSUMED)."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

import numpy as np
from scipy.signal import welch
from scipy.stats import poisson, t

from zlscan.config.presets import demo_config
from zlscan.engine.chain import Chain
from zlscan.engine.validity import physical_applicability
from zlscan.truth.detector import C, E, H
from zlscan.truth.psf import PsfConfig
from zlscan.truth.scene import uniform


def config(os: int, response: float | None, seed: int):
    cfg = demo_config(seed)
    cfg.os = os
    power = 1e8 * H * C / (cfg.detector.wavelength_nm * 1e-9)
    power /= cfg.detector.qe * cfg.collect.eta_pre * cfg.collect.eta_collect
    cfg.source = replace(cfg.source, power_w=power, gated=False)
    cfg.detector = replace(
        cfg.detector, excess_noise_factor=1, dark_current_a=0, pole_hz=response, anode_limit_a=None
    )
    cfg.afe = replace(cfg.afe, polarity=1, pole_hz=30_000, aa_poles_hz=(50_000,))
    cfg.psf = PsfConfig(0, 0)
    cfg.scenes = {"main": uniform(0, cfg.scan.width_m, 1)}
    return cfg


def interval(values):
    values = np.asarray(values)
    mean = float(values.mean())
    half = float(t.ppf(0.975, len(values) - 1) * values.std(ddof=1) / np.sqrt(len(values)))
    return {"mean": mean, "replicate_mean_ci95": [mean - half, mean + half]}


def audit():
    rows, bias = [], []
    for os in (4, 8, 16, 32):
        dt = 1 / (1e6 * os)
        count = np.arange(300)
        volts = 0.25 * np.log10(np.maximum(count * E * 1000 / dt, 1e-12))
        delta = np.sum(poisson.pmf(count, 1e8 * dt) * volts) - 0.25 * np.log10(1e8 * E * 1000)
        bias.append({"os": os, "dt_s": dt, "mean_counts": 1e8 * dt, "bias_mv": float(1000 * delta)})
        for pole in (None, 20_000):
            replicates, spectra = [], []
            for seed in (41, 42, 43):
                cfg = config(os, pole, seed)
                chain = Chain(cfg)
                chain.probe_names = {"v_adc"}
                voltage = chain.run_steps(8192 * os, 1537).probes["v_adc"][::os][512:]
                frequency, psd = welch(voltage, fs=cfg.fs_hz, nperseg=512)
                band = frequency <= 50_000
                band_variance = np.sum(psd[band]) * (frequency[1] - frequency[0])
                replicates.append(
                    {
                        "seed": seed,
                        "mean_v": float(voltage.mean()),
                        "variance_v2": float(voltage.var()),
                        "band_0_50khz_variance_v2": float(band_variance),
                    }
                )
                spectra.append(psd)
            rows.append(
                {
                    "os": os,
                    "input_response_hz": pole,
                    "evidence": "assumed",
                    "replicates": replicates,
                    "mean_v": interval([r["mean_v"] for r in replicates]),
                    "variance_v2": interval([r["variance_v2"] for r in replicates]),
                    "band_0_50khz_variance_v2": interval(
                        [r["band_0_50khz_variance_v2"] for r in replicates]
                    ),
                    "frequency_hz": frequency.tolist(),
                    "psd_v2_per_hz": np.mean(spectra, axis=0).tolist(),
                    "applicability": physical_applicability(cfg),
                }
            )
            print(f"completed os={os}, assumed input response={pole}", flush=True)
    return {
        "kind": "assumed_model_statistical_audit",
        "primary_electron_rate_s": 1e8,
        "adc_rate_hz": 1e6,
        "seeds": [41, 42, 43],
        "samples_per_replicate": 8192,
        "discarded_warmup_samples": 512,
        "post_log_poles_hz": [30_000, 50_000],
        "exact_unfiltered_poisson_bias": bias,
        "grid_experiments": rows,
        "note": "Relative to log(mean current) is a diagnostic, "
        "not an assertion of real circuit response.",
        "remaining_validation": [
            "low-light step grid statistics",
            "independent calibration residuals",
            "measured PMT and input-node response",
            "measured device density capability",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    result = audit()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, indent=2, allow_nan=False) + "\n")
