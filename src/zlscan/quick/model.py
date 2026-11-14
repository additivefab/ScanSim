"""Vectorized approximate scanner model (SIMULATED/APPROXIMATE only)."""

from __future__ import annotations

import csv
import json
import math
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter
from scipy.signal import lfilter


@dataclass
class QuickConfig:
    width: int = 512
    height: int = 256
    scene_width_m: float = 0.08
    scene_height_m: float = 0.04
    density_min: float = 0.0
    density_max: float = 4.0
    invert: bool = False
    spot_fwhm_m: float = 0.0008
    glare_weight: float = 0.0
    glare_fwhm_m: float = 0.003
    laser_rel: float = 1.0
    pmt_gain_rel: float = 1.0
    tau_s: float = 0.00002
    background_rel: float = 0.02
    defect_amplitude: float = 0.0
    defect_center: float = 0.5
    defect_sigma: float = 0.04
    noise_sigma_v: float = 0.0
    feed_speed_m_s: float = 0.08
    facets: int = 4
    rpm: float = 6000.0
    focal_m: float = 0.1
    seed: int = 1
    adc_bits: int = 16
    adc_vmin: float = 0.0
    adc_vmax: float = 2.5
    log_offset_v: float = 1.0
    log_slope_v_per_decade: float = 0.25
    log_i_ref_rel: float = 1.0
    log_floor_rel: float = 1e-6
    input_path: str | None = None
    case: str = "custom"
    advanced: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: dict) -> QuickConfig:
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in allowed})

    def validate(self) -> None:
        ints = {
            "width": self.width,
            "height": self.height,
            "facets": self.facets,
            "adc_bits": self.adc_bits,
        }
        if any(int(v) != v or int(v) <= 0 for v in ints.values()):
            raise ValueError("quick dimensions, facets and adc_bits must be positive integers")
        if self.width > 4096 or self.height > 4096:
            raise ValueError("quick preview is limited to 4096x4096")
        if self.scene_width_m <= 0 or self.scene_height_m <= 0:
            raise ValueError("scene dimensions must be positive")
        if self.spot_fwhm_m < 0 or self.glare_fwhm_m < 0 or not 0 <= self.glare_weight <= 1:
            raise ValueError("spot/glare parameters are outside their ranges")
        if self.tau_s < 0 or self.noise_sigma_v < 0 or self.defect_sigma <= 0:
            raise ValueError("tau, noise and defect parameters are invalid")
        if self.adc_vmax <= self.adc_vmin or self.log_slope_v_per_decade == 0:
            raise ValueError("ADC range and log slope must be non-degenerate")


@dataclass
class QuickResult:
    run_dir: Path
    metadata: dict
    arrays: dict[str, np.ndarray]


def _builtin_density(cfg: QuickConfig) -> np.ndarray:
    y, x = np.mgrid[0 : cfg.height, 0 : cfg.width]
    wedge = cfg.density_min + (x / max(cfg.width - 1, 1)) * (cfg.density_max - cfg.density_min)
    stripe = 0.18 * np.sin(2 * np.pi * y / max(20, cfg.height // 8))
    return np.clip(wedge + stripe, cfg.density_min, cfg.density_max)


def load_density(cfg: QuickConfig) -> tuple[np.ndarray, dict]:
    if cfg.input_path:
        path = Path(cfg.input_path).expanduser()
        if path.suffix.lower() == ".npy":
            density = np.asarray(np.load(path), dtype=float)
            if density.ndim != 2:
                raise ValueError("density .npy must be a 2-D array")
            density = np.where(np.isfinite(density), density, np.nan)
        else:
            with Image.open(path) as image:
                gray = image.convert("L").resize((cfg.width, cfg.height), Image.Resampling.BILINEAR)
                g = np.asarray(gray, dtype=float) / 255.0
            if cfg.invert:
                g = 1.0 - g
            density = cfg.density_min + (1.0 - g) * (cfg.density_max - cfg.density_min)
        return density, {
            "input": str(path),
            "input_kind": "density_npy" if path.suffix.lower() == ".npy" else "grayscale_image",
        }
    return _builtin_density(cfg), {"input": "builtin_demo", "input_kind": "synthetic_density"}


def _preview(values: np.ndarray, path: Path, low: float = 0.0, high: float = 4.0) -> None:
    finite = np.nan_to_num(values, nan=high, posinf=high, neginf=low)
    pixels = np.clip((finite - low) / max(high - low, 1e-12) * 255, 0, 255).astype(np.uint8)
    Image.fromarray(pixels, mode="L").save(path)


def _git_head() -> str | None:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def run_quick(cfg: QuickConfig, out: Path) -> QuickResult:
    cfg.validate()
    out = Path(out).resolve()
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"quick output already exists: {out}")
    out.mkdir(parents=True, exist_ok=False)
    density, input_meta = load_density(cfg)
    finite = np.isfinite(density)
    if not finite.any():
        raise ValueError("input has no finite density values")
    density = np.where(finite, density, np.nan)
    spacing_x = cfg.scene_width_m / cfg.width
    spacing_y = cfg.scene_height_m / cfg.height
    trans = np.where(finite, 10.0 ** (-density), np.nan)
    sigma_x = cfg.spot_fwhm_m / 2.35482 / spacing_x
    sigma_y = cfg.spot_fwhm_m / 2.35482 / spacing_y
    core = (
        gaussian_filter(np.nan_to_num(trans, nan=1.0), sigma=(sigma_y, sigma_x), mode="nearest")
        if cfg.spot_fwhm_m
        else trans.copy()
    )
    if cfg.glare_weight:
        tail = gaussian_filter(
            np.nan_to_num(trans, nan=1.0),
            sigma=(
                cfg.glare_fwhm_m / 2.35482 / spacing_y,
                cfg.glare_fwhm_m / 2.35482 / spacing_x,
            ),
            mode="nearest",
        )
        teff = (1 - cfg.glare_weight) * core + cfg.glare_weight * tail
    else:
        teff = core
    x = (np.arange(cfg.width) + 0.5) / cfg.width
    profile = 1.0 - cfg.defect_amplitude * (2 * x - 1) ** 2
    if cfg.defect_amplitude:
        profile += cfg.defect_amplitude * np.exp(
            -((x - cfg.defect_center) ** 2) / (2 * cfg.defect_sigma**2)
        )
    profile = np.maximum(profile, 0.0)
    f_line = cfg.facets * cfg.rpm / 60.0
    if f_line <= 0 or cfg.feed_speed_m_s <= 0:
        raise ValueError("rpm, facets and feed speed must be positive")
    line_period = 1.0 / f_line
    active = cfg.scene_width_m / (4 * math.pi * cfg.focal_m * cfg.rpm / 60.0)
    if active >= line_period:
        raise ValueError("scan active time exceeds line period; adjust rpm/focal/scene width")
    dt_eff = active / cfg.width
    times = (
        np.arange(cfg.height, dtype=float)[:, None] * line_period
        + (line_period - active) / 2
        + (np.arange(cfg.width) + 0.5) * dt_eff
    )
    y_positions = times * cfg.feed_speed_m_s
    row_indices = np.clip(
        np.rint(y_positions / max(cfg.scene_height_m, 1e-12) * (cfg.height - 1)).astype(int),
        0,
        cfg.height - 1,
    )
    p_rel = (
        cfg.laser_rel * teff[row_indices, np.arange(cfg.width)[None, :]] * profile[None, :]
        + cfg.background_rel
    )
    i_rel = cfg.pmt_gain_rel * p_rel
    target = cfg.log_offset_v + cfg.log_slope_v_per_decade * np.log10(
        np.maximum(i_rel, cfg.log_floor_rel) / cfg.log_i_ref_rel
    )
    voltage = np.empty_like(target, dtype=float)
    alpha = math.exp(-dt_eff / cfg.tau_s) if cfg.tau_s else 0.0
    rng = np.random.default_rng(cfg.seed)
    previous = cfg.log_offset_v + cfg.log_slope_v_per_decade * math.log10(
        max(cfg.background_rel, cfg.log_floor_rel) / cfg.log_i_ref_rel
    )
    for row in range(cfg.height):
        if cfg.tau_s:
            previous = cfg.log_offset_v + (previous - cfg.log_offset_v) * math.exp(
                -max(line_period - active, 0.0) / cfg.tau_s
            )
            voltage[row], state = lfilter(
                [1 - alpha], [1, -alpha], target[row], zi=[alpha * previous]
            )
            previous = float(voltage[row, -1])
        else:
            voltage[row] = target[row]
    if cfg.noise_sigma_v:
        voltage = voltage + rng.normal(0.0, cfg.noise_sigma_v, voltage.shape)
    lsb = (cfg.adc_vmax - cfg.adc_vmin) / (2**cfg.adc_bits)
    saturated = (voltage < cfg.adc_vmin) | (voltage >= cfg.adc_vmax)
    code = np.clip(np.floor((voltage - cfg.adc_vmin) / lsb), 0, 2**cfg.adc_bits - 1).astype(
        np.uint16
    )
    inv_i = cfg.log_i_ref_rel * 10 ** ((voltage - cfg.log_offset_v) / cfg.log_slope_v_per_decade)
    that = (inv_i / cfg.pmt_gain_rel - cfg.background_rel) / np.maximum(cfg.laser_rel, 1e-12)
    with np.errstate(divide="ignore", invalid="ignore"):
        density_hat = np.where(that > 0, -np.log10(that), np.nan)
    valid = (
        finite[row_indices, np.arange(cfg.width)[None, :]] & ~saturated & np.isfinite(density_hat)
    )
    density_hat = np.where(valid, density_hat, np.nan)
    arrays = {
        "density_input": density.astype(np.float32),
        "transmittance_effective": teff.astype(np.float32),
        "power_rel": p_rel.astype(np.float32),
        "current_rel": i_rel.astype(np.float32),
        "voltage_v": voltage.astype(np.float32),
        "code": code,
        "density_hat": density_hat.astype(np.float32),
        "valid": valid,
        "saturated": saturated,
        "time_s": times.astype(np.float64),
        "x_m": np.broadcast_to(
            (np.arange(cfg.width) + 0.5) * spacing_x, (cfg.height, cfg.width)
        ).astype(np.float64),
        "y_m": y_positions.astype(np.float64),
    }
    np.savez_compressed(out / "arrays.npz", **arrays)
    _preview(density_hat, out / "preview.png", cfg.density_min, cfg.density_max)
    line = cfg.height // 2
    with (out / "selected_line.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "quick_sample_index",
                "t_s",
                "x_m",
                "y_m",
                "power_rel",
                "current_rel",
                "voltage_v",
                "code",
                "density_hat",
                "valid",
                "saturated",
            ]
        )
        for col in range(cfg.width):
            writer.writerow(
                [
                    col,
                    times[line, col],
                    arrays["x_m"][line, col],
                    y_positions[line, col],
                    p_rel[line, col],
                    i_rel[line, col],
                    voltage[line, col],
                    int(code[line, col]),
                    density_hat[line, col],
                    bool(valid[line, col]),
                    bool(saturated[line, col]),
                ]
            )
    metadata = {
        "format": "zlscan.quick-run/1",
        "mode": "quick",
        "origin": "SIMULATED",
        "fidelity": "APPROXIMATE",
        "validation_status": "NOT_VALIDATED",
        "validated_for_hardware": False,
        "reconstruction": "nominal_model_inverse",
        "geometry_reconstruction": "row_stacking_approximate",
        "seed": cfg.seed,
        "git_head": _git_head(),
        "config": asdict(cfg),
        "input": input_meta,
        "shape": [cfg.height, cfg.width],
        "valid_fraction": float(np.mean(valid)),
        "saturated_fraction": float(np.mean(saturated)),
        "representative_time_step_s": dt_eff,
        "line_period_s": line_period,
        "notes": [
            "No real ADC clock or digital link is simulated.",
            "Noise is equivalent voltage demonstration noise.",
            "Image density mapping and inverse are nominal assumptions.",
        ],
    }
    (out / "quick_run.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return QuickResult(out, metadata, arrays)
