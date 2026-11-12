"""Legacy behaviour check for Q013: package B's deterministic ideal regression on the new chain.

Package B's ``ideal_regression`` configuration switches off noise, blur, analogue poles and
distortion and keeps the 16-bit quantisation. With the same film, optical power, detector, log law
and ADC, the new forward chain must reconstruct the density with the same error. The
reconstruction here is the legacy one, **with the true parameters** (model-known dark and flat);
it is an evaluator, not the observable-only reconstruction of stage 4, which is why it lives in
``metrics`` (allowed to read the truth) and not in ``recon``.

Timing is mapped onto the new scan model as follows (these are fitting choices, not device data):
the line period is 512 ADC samples, one facet, so ``rpm = 60 * fs / 512``; the focal length is
chosen so the spot moves one pitch per ADC sample; the active window starts half a sample before
ADC sample ``pre_samples`` so that sample ``pre + j`` falls on pixel ``j``'s centre, exactly as in
the legacy code; the feed is step-and-hold with each line centred on its pixel row.

"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

import numpy as np

from zlscan.engine.chain import Chain, ChainConfig
from zlscan.evidence.legacy_runner import B_DELIVERED, LEGACY_ROOT, delivered_metrics
from zlscan.truth import adc, afe, collect, detector, psf, scan, source
from zlscan.truth.detector import C, E, H
from zlscan.truth.scene import Scene

CONFIG = LEGACY_ROOT / "package_b" / "configs" / "ideal_regression.json"
DELIVERED_CODES = LEGACY_ROOT / "package_b/examples/ideal_regression/raw_roi_u16le.bin"
OWN_TEST_LIMIT = 3e-5  # package B's own unittest asserts density_rmse_unblurred < 3e-5


def _legacy_phantom(nx: int, ny: int, dmax: float) -> np.ndarray:
    """Package B's synthetic density target, rewritten here (legacy/ stays read-only)."""
    x = np.arange(nx)[None, :]
    y = np.arange(ny)[:, None]
    v = np.broadcast_to(0.12 + (dmax - 0.24) * x / max(1, nx - 1), (ny, nx)).copy()
    v[: ny // 3] = 0.08 + (dmax - 0.16) * np.floor(np.minimum(x * 10 / nx, 9)) / 9
    mid = (y >= ny // 3) & (y < 2 * ny // 3)
    v = np.where(mid, np.where(x > nx * 0.48 + 0.12 * (y - ny / 2), 0.3, dmax - 0.3), v)
    bars = (y >= 2 * ny // 3) & (x < nx * 0.55)
    v = np.where(bars, 0.35 + (dmax - 0.7) * ((x // max(2, nx // 64)) % 2), v)
    circle = ((x - nx * 0.78) / (nx * 0.08)) ** 2 + ((y - ny * 0.8) / (ny * 0.12)) ** 2 < 1
    return np.clip(np.where(circle, dmax, v), 0, dmax)


def _ideal_chain_config(c: dict) -> tuple[ChainConfig, np.ndarray]:
    s, o, p, a, d = (c[k] for k in ("scan", "optics", "pmt", "analog", "adc"))
    if s["samples_per_pixel"] != 1 or s["y_mode"] != "step_hold" or p["noise_enabled"]:
        raise ValueError("only the deterministic one-sample-per-pixel step-and-hold case is mapped")
    fs, nl, pre, nx, ny = int(s["fs_hz"]), s["line_samples"], s["pre_samples"], s["nx"], s["ny"]
    pitch_x, pitch_y = s["pitch_x_um"] * 1e-6, s["pitch_y_um"] * 1e-6
    rpm = 60.0 * fs / nl
    focal = pitch_x * fs / (2 * (2 * math.pi * rpm / 60))
    den = _legacy_phantom(nx, ny, c["film"]["density_max"])
    cfg = ChainConfig(
        seed=c["seed"],
        fs_hz=fs,
        os=int(s["oversample"]),
        scan=scan.ScanConfig(
            facets=1,
            rpm=rpm,
            focal_m=focal,
            pitch_x_m=pitch_x,
            pitch_y_m=pitch_y,
            nx=nx,
            x_center_m=nx * pitch_x / 2,
            y0_m=pitch_y / 2,
            active_offset_s=(pre - 0.5) / fs,
            continuous_feed=False,
        ),
        source=source.SourceConfig(power_w=o["laser_power_W"], gated=False),
        collect=collect.CollectConfig(o["pre_film_efficiency"], o["collection_efficiency"]),
        psf=psf.PsfConfig(0.0, 0.0),
        detector=detector.DetectorConfig(
            p["qe"],
            p["gain"],
            p["excess_noise_factor"],
            p["dark_anode_A"],
            o["wavelength_nm"],
            p["anode_limit_A"],
            shot_noise=False,
        ),
        afe=afe.AfeConfig(
            kind="log",
            polarity=-1,
            v0=a["offset_V"],
            slope_v_per_dec=a["log_slope_V_dec"],
            i_ref_a=a["reference_current_A"],
            i_floor_a=a["current_floor_A"],
        ),
        adc=adc.AdcConfig(d["bits"], d["vmin"], d["vmax"]),
        scenes={"main": Scene.from_density(den, nx * pitch_x, ny * pitch_y)},
        evidence={"all": "assumed (package B ideal_regression, timing fitted to the scan model)"},
    )
    return cfg, den


def _true_parameter_density(codes: np.ndarray, c: dict) -> np.ndarray:
    """Legacy reconstruction with model-known dark and flat: code -> V -> I -> T -> D."""
    o, p, a, d = (c[k] for k in ("optics", "pmt", "analog", "adc"))
    v = d["vmin"] + (codes.astype(float) + 0.5) * (d["vmax"] - d["vmin"]) / 2 ** d["bits"]
    v = (v - a["adc_driver_offset_V"]) / a["adc_driver_gain"]
    decades = np.clip((a["offset_V"] - v) / a["log_slope_V_dec"], -20, 20)
    cur = a["reference_current_A"] * 10**decades
    photon = H * C / (o["wavelength_nm"] * 1e-9)
    power0 = o["laser_power_W"] * o["pre_film_efficiency"] * o["collection_efficiency"]
    flat = E * p["gain"] * (power0 / photon * p["qe"])  # same order as the legacy code
    trans = (cur - p["dark_anode_A"]) / flat
    return -np.log10(np.maximum(trans, 1e-8))


@dataclass(frozen=True)
class RegressionResult:
    rmse_new: float
    rmse_delivered: float
    bias_new: float
    codes_shape: tuple[int, int]
    differing_codes: int


def run_ideal_regression() -> RegressionResult:
    """Compare the V5 chain with the frozen package B ideal scenario."""
    c = json.loads(CONFIG.read_text(encoding="utf-8"))
    cfg, den = _ideal_chain_config(c)
    s = c["scan"]
    nl, pre, nx, ny = s["line_samples"], s["pre_samples"], s["nx"], s["ny"]
    out = Chain(cfg).run_steps(ny * nl * cfg.os, nl * cfg.os)
    codes = out.codes.reshape(ny, nl)[:, pre : pre + nx]
    delivered_codes = np.fromfile(DELIVERED_CODES, dtype="<u2").reshape(ny, nx)
    errors = _true_parameter_density(codes, c) - den
    delivered_rmse = delivered_metrics(B_DELIVERED["ideal_regression"])["density_rmse_unblurred"]
    return RegressionResult(
        rmse_new=float(np.sqrt(np.mean(errors**2))),
        rmse_delivered=float(delivered_rmse),
        bias_new=float(np.mean(errors)),
        codes_shape=(ny, nx),
        differing_codes=int(np.count_nonzero(codes != delivered_codes)),
    )
