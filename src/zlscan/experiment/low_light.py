"""Assumed stochastic validation fixtures; fit/reconstruction consume reread ADC observations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import numpy as np
from scipy.signal import welch

from zlscan.calib.radiometric import fit_log_transfer
from zlscan.config.presets import demo_config
from zlscan.contracts.calibration import Binding, CalibItem, CalibrationHeader
from zlscan.contracts.flags import InvalidFlag
from zlscan.engine.chain import Chain
from zlscan.io.manifest import sha256_file
from zlscan.io.rawstream import (
    FLAG_EPOCH_START,
    FLAG_LINE_END,
    FLAG_LINE_START,
    RawWriter,
    SessionHeader,
    scan_blocks,
)
from zlscan.recon.density import Grid, reconstruct_density
from zlscan.truth.detector import C, H
from zlscan.truth.psf import PsfConfig
from zlscan.truth.scene import uniform


@dataclass(frozen=True)
class CapturePhase:
    name: str
    electron_rate_s: float
    shape: tuple[int, int]


def fixture(os: int, seed: int, assumptions: dict):
    cfg = demo_config(seed)
    cfg.os = os
    cfg.detector = replace(
        cfg.detector,
        excess_noise_factor=assumptions["excess_noise_factor"],
        dark_current_a=0,
        pole_hz=assumptions["input_response_hz"],
        anode_limit_a=None,
    )
    cfg.afe = replace(
        cfg.afe,
        polarity=1,
        pole_hz=assumptions["post_log_poles_hz"][0],
        aa_poles_hz=tuple(assumptions["post_log_poles_hz"][1:]),
    )
    cfg.adc = replace(
        cfg.adc, vmin=assumptions["adc_range_v"][0], vmax=assumptions["adc_range_v"][1]
    )
    cfg.source = replace(cfg.source, gated=False)
    cfg.collect = replace(cfg.collect, rolloff=0, stray_w=0)
    cfg.scenes = {"main": uniform(0, cfg.scan.width_m, 1)}
    cfg.psf = PsfConfig(0, 0)
    cfg.evidence = {"all": "ASSUMED stochastic validation fixture; not a device working point"}
    return cfg


def study_binding(cfg) -> Binding:
    # A named study instrument excludes numerical mesh and realization, not any physical setting.
    # It intentionally cannot match production run bindings, whose mesh remains part of the hash.
    canonical = replace(
        cfg, os=2, seed=0, scenes={}, timeline=[], injections=[], evidence={}
    ).hash()
    return Binding(simulation_hash="grid-study:" + canonical, channel_roles={"0": None})


def _set_rate(chain: Chain, rate: float) -> None:
    cfg = chain.cfg
    power = rate * H * C / (cfg.detector.wavelength_nm * 1e-9)
    power /= cfg.detector.qe * cfg.collect.eta_pre * cfg.collect.eta_collect
    cfg.source = replace(cfg.source, power_w=power)


def write_capture(
    path: Path,
    cfg,
    phases: list[CapturePhase],
    *,
    settle_samples: int,
    prewarm_samples: int = 0,
    prewarm_rate_s: float = 0,
) -> dict:
    """Persist only ADC words; physical settling gaps keep their actual source sample indices."""
    chain = Chain(replace(cfg))
    if prewarm_samples:
        _set_rate(chain, prewarm_rate_s)
        chain.advance(prewarm_samples * cfg.os)
    captured, protocol = [], []
    for phase in phases:
        _set_rate(chain, phase.electron_rate_s)
        if settle_samples:
            chain.advance(settle_samples * cfg.os)
        output = chain.advance(int(np.prod(phase.shape)) * cfg.os)
        captured.append(output.codes.reshape(phase.shape))
        protocol.append(
            {
                **asdict(phase),
                "first_sample_index": str(output.first_sample),
                "adc_saturated_samples": int(output.saturated.sum()),
                "front_end_clipped_samples": int(output.front_end_clipped.sum()),
            }
        )
    header = SessionHeader(
        schema_version="0.1",
        session_id=1,
        created_at="assumed-study",
        hardware_hash=None,
        firmware_hash=None,
        wiring_rev=None,
        register_dump_hash=None,
        calibration_id=None,
        channel_roles={"0": None},
        clock_domains=[asdict(chain.adc_clock)],
        coding="offset_binary",
        bits_per_sample=cfg.adc.bits,
        bytes_per_sample=2,
        origin="SIMULATED",
        windows={
            "kind": "stationary_photometric_study",
            "retention": "explicit measurement windows; settling is not retained",
            "settle_samples": settle_samples,
            "prewarm_samples": prewarm_samples,
            "prewarm_rate_s": prewarm_rate_s,
            "phases": protocol,
            "numeric_grid_os": cfg.os,
            "seed": cfg.seed,
            "config_hash": cfg.hash(),
            "study_binding": study_binding(cfg).model_dump(mode="json"),
        },
    )
    with RawWriter(path, header) as writer:
        for i, (data, phase) in enumerate(zip(captured, protocol, strict=True)):
            writer.write_block(
                epoch=0,
                channel_mask=1,
                clock_domain=cfg.domain_id,
                first_sample_index=int(phase["first_sample_index"]),
                samples=data.reshape(-1, 1),
                line_number=i,
                flags=FLAG_LINE_START | FLAG_LINE_END | (FLAG_EPOCH_START if i == 0 else 0),
            )
        writer.checkpoint()
    return {
        "path": path.name,
        "sha256": sha256_file(path),
        "seed": cfg.seed,
        "config_hash": cfg.hash(),
        "numeric_grid_os": cfg.os,
        "phases": protocol,
    }


def read_capture(path: Path) -> tuple[dict[str, np.ndarray], dict]:
    data = {}

    def consume(session, block, samples):
        phase = session.windows["phases"][block.line_number]
        if block.first_sample_index != int(phase["first_sample_index"]) or phase["name"] in data:
            raise ValueError("study phase identity is inconsistent")
        data[phase["name"]] = samples[:, 0].reshape(phase["shape"]).copy()

    result = scan_blocks(path, consume)
    if (
        result.fatal
        or result.errors
        or result.losses
        or len(data) != len(result.session.windows["phases"])
    ):
        raise ValueError("study raw capture is incomplete or corrupt")
    return data, result.session.windows


def fit_reference(data: dict[str, np.ndarray], densities, binding: Binding, identifier: str):
    """No generator configuration or validation labels enter this observation-only fit."""
    wedge = np.concatenate([data[f"wedge_{i}"] for i in range(len(densities))], axis=1)
    fit = fit_log_transfer(data["dark"], data["white"], wedge, np.asarray(densities))
    calibration = CalibrationHeader(
        format_version=1,
        provenance="SIMULATED",
        calibration_id=identifier,
        created_at="assumed-study",
        binds_to=binding,
        generator={
            "method": "fit_log_transfer",
            "reference_density": list(densities),
            "scope": "stationary grid study; not a production run calibration",
        },
        items={
            "transfer_lut": CalibItem(
                value={
                    "channels": {
                        "0": {
                            "code_knots": fit.code_knots,
                            "intensity_knots": fit.intensity_knots,
                            "white_outlier_columns": fit.white_outlier_columns,
                        }
                    }
                }
            ),
            "dark_raw_code": CalibItem(
                value={"0": {"median": np.median(data["dark"], axis=0).tolist()}}
            ),
            "white_raw_code": CalibItem(
                value={"0": {"median": np.median(data["white"], axis=0).tolist()}}
            ),
        },
    )
    return calibration, asdict(fit)


def evaluate_codes(
    codes: np.ndarray,
    calibration: CalibrationHeader,
    binding: Binding,
    *,
    expected_density: float,
    pitch_m: float,
    bits: int,
) -> dict:
    """Use declared analysis bins; spatial geometry is not inferred from the physical generator."""
    rows, width = codes.shape
    grid = Grid(width, 1, pitch_m, pitch_m)
    x = np.broadcast_to((np.arange(width) + 0.5) * pitch_m, (rows, width))
    y = np.full(codes.shape, 0.5 * pitch_m)
    flags = np.where(
        (codes == 0) | (codes == 2**bits - 1), int(InvalidFlag.ADC_SATURATED), 0
    ).astype(np.uint16)
    image = reconstruct_density(
        codes, x, y, grid, calibration, binding, channel=0, sample_flags=flags
    )
    valid = image.flags == 0
    residual = image.density[valid] - expected_density
    return {
        "bias_od_valid_only": float(residual.mean()) if residual.size else None,
        "rmse_od_valid_only": float(np.sqrt(np.mean(residual**2))) if residual.size else None,
        "invalid_fraction": 1 - image.valid_fraction,
        "valid_pixels": int(valid.sum()),
        "total_pixels": width,
        "invalid_reason_counts": {
            flag.name: int(np.sum((image.flags & int(flag)) != 0)) for flag in InvalidFlag
        },
        "mean_transmittance_valid_only": float(image.transmittance[valid].mean())
        if residual.size
        else None,
    }


def step_metrics(data, *, cycles: int, phase_samples: int, fs_hz: int, adc_range_v, bits: int):
    down = np.stack([data[f"down_{i}"] for i in range(cycles)]).reshape(cycles, phase_samples)
    up = np.stack([data[f"up_{i}"] for i in range(cycles)]).reshape(cycles, phase_samples)
    waves, metrics, spectra = {}, {}, {}
    for name, codes in (("down", down), ("up", up)):
        volts = adc_range_v[0] + codes.astype(float) / (2**bits - 1) * (
            adc_range_v[1] - adc_range_v[0]
        )
        waves[name] = volts.mean(axis=0).tolist()
        metrics[f"{name}_initial_mean_v"] = float(volts[:, : phase_samples // 4].mean())
        metrics[f"{name}_plateau_mean_v"] = float(volts[:, phase_samples // 2 :].mean())
        freq, psd = welch(
            volts[:, phase_samples // 2 :], fs=fs_hz, nperseg=phase_samples // 2, axis=1
        )
        mean_psd = psd.mean(axis=0)
        metrics[f"{name}_windowed_band_variance_v2"] = float(
            mean_psd[freq <= 50_000].sum() * (freq[1] - freq[0])
        )
        spectra[name] = {"frequency_hz": freq.tolist(), "psd_v2_per_hz": mean_psd.tolist()}
    return metrics, waves, spectra


def digest_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()
