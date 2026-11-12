"""Explicitly assumed scenario input shared by public run and budget commands."""

from __future__ import annotations

import hashlib
import io
import json
import math
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any, Literal, get_type_hints

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from zlscan.config.presets import demo_config, nominal_config
from zlscan.engine.chain import MODEL_VERSION, ChainConfig, Phase
from zlscan.engine.config_bundle import save_config_bundle
from zlscan.truth.scene import Scene


class SceneInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    width_m: float = Field(gt=0)
    height_m: float = Field(gt=0)
    off_film_T: float = Field(ge=0, le=1)
    density: list[list[float]] | None = None
    transmittance_npy: str | None = None
    sha256: str | None = None

    @model_validator(mode="after")
    def source(self):
        if (self.density is None) == (self.transmittance_npy is None):
            raise ValueError("scene requires exactly one density or transmittance_npy")
        if (self.transmittance_npy is not None) != (self.sha256 is not None):
            raise ValueError("NPY scene requires SHA256; inline scene has no file SHA")
        return self


class ScenarioInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    format: Literal["zlscan.research-scenario/1"]
    model_version: Literal[MODEL_VERSION]
    preset: Literal["demo", "nominal"]
    lines: int = Field(gt=0)
    chain: dict[str, Any] = Field(default_factory=dict)
    scenes: dict[str, SceneInput] | None = None
    timeline: list[dict[str, Any]] | None = None
    secondary: dict[str, Any] | None = None


@dataclass(frozen=True)
class LoadedScenario:
    document: ScenarioInput
    original: bytes
    primary: ChainConfig
    secondary: ChainConfig | None

    def provenance(self) -> dict:
        return dict(
            format=self.document.format,
            evidence="assumed",
            input_sha256=hashlib.sha256(self.original).hexdigest(),
            primary_hash=self.primary.hash(),
            secondary_hash=self.secondary.hash() if self.secondary else None,
        )


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError(f"duplicate JSON key {key}")
        result[key] = value
    return result


def _patch(cfg: ChainConfig, patch: dict) -> ChainConfig:
    hints = get_type_hints(ChainConfig)
    forbidden = {"scenes", "timeline", "evidence"}
    if set(patch) - (set(hints) - forbidden):
        raise ValueError("unknown or reserved chain field")
    changes = {}
    for name, value in patch.items():
        original = getattr(cfg, name)
        if name in {"scan", "source", "collect", "psf", "detector", "afe", "adc"}:
            if not isinstance(value, dict) or set(value) - {f.name for f in fields(original)}:
                raise ValueError(f"unknown fields in {name}")
            changes[name] = TypeAdapter(type(original)).validate_json(
                json.dumps(asdict(original) | value, allow_nan=False), strict=True
            )
        else:
            changes[name] = TypeAdapter(hints[name]).validate_json(
                json.dumps(value, allow_nan=False), strict=True
            )
    return replace(cfg, **changes)


def _validate(cfg: ChainConfig, lines: int) -> None:
    sc, so, co, de, af, ad, ps = (
        getattr(cfg, k) for k in ["scan", "source", "collect", "detector", "afe", "adc", "psf"]
    )
    positive = [
        cfg.fs_hz,
        sc.facets,
        sc.rpm,
        sc.focal_m,
        sc.pitch_x_m,
        sc.pitch_y_m,
        sc.nx,
        de.gain,
        de.wavelength_nm,
        af.i_ref_a,
        af.i_floor_a,
        af.knee_a,
        af.floor_hz,
        ps.nodes_x,
        ps.nodes_y,
    ]
    nonnegative = [
        cfg.seed,
        sc.row_jitter_s,
        so.power_w,
        so.ripple_hz,
        so.line_sigma,
        co.stray_w,
        de.dark_current_a,
        ps.fwhm_x_m,
        ps.fwhm_y_m,
        ad.jitter_rms_s,
    ]
    if min(positive) <= 0 or min(nonnegative) < 0:
        raise ValueError("positive/nonnegative physical parameter out of range")
    if not all(
        math.isfinite(v) and v > 0
        for v in [sc.f_line, sc.v_x, sc.width_m, sc.line_period_s, sc.active_s]
    ):
        raise ValueError("invalid derived scan geometry")
    if cfg.os < 2 or cfg.os % 2 or cfg.domain_id < 0 or cfg.domain_id > 65535:
        raise ValueError("invalid oversampling or clock domain")
    if round(sc.line_period_s * cfg.fs_hz * cfg.os) < 1:
        raise ValueError("line duration is below one fine step")
    if ad.bits < 1 or ad.bits > 16 or ad.vmax <= ad.vmin:
        raise ValueError("ADC requires 1..16 bits and increasing voltage range")
    if af.kind not in {"log", "linear"} or af.polarity not in {-1, 1}:
        raise ValueError("invalid AFE kind or polarity")
    if af.kind == "log" and af.slope_v_per_dec <= 0:
        raise ValueError("log slope must be positive")
    if any(v is not None and v <= 0 for v in [af.pole_hz, de.pole_hz, de.anode_limit_a]):
        raise ValueError("response poles and current limit must be positive or null")
    if any(v <= 0 for v in af.aa_poles_hz):
        raise ValueError("AA poles must be positive")
    if af.clip_min_v is not None and af.clip_max_v is not None and af.clip_min_v >= af.clip_max_v:
        raise ValueError("clip voltage range must increase")
    if not all(0 <= v <= 1 for v in [de.qe, co.eta_pre, co.eta_collect, co.rolloff, so.ripple]):
        raise ValueError("efficiencies, rolloff and ripple require 0..1")
    if de.excess_noise_factor < 1:
        raise ValueError("excess noise factor requires >= 1")
    if any(a and len(a) != sc.facets for a in [sc.facet_offset_m, sc.facet_gain]):
        raise ValueError("facet arrays must match facet count")
    if sc.facet_gain and min(sc.facet_gain) <= 0:
        raise ValueError("facet gains must be positive")
    if sc.window_start_s() < 0 or sc.window_start_s() + sc.active_s > sc.line_period_s:
        raise ValueError("capture window outside line period")
    if not cfg.scenes:
        raise ValueError("scene table cannot be empty")
    stop = 0
    for phase in cfg.timeline:
        if (
            phase.first_line != stop
            or phase.n_lines <= 0
            or phase.scene not in cfg.scenes
            or not phase.name
        ):
            raise ValueError("timeline must be ordered, contiguous and reference known scenes")
        stop += phase.n_lines
    if cfg.timeline and stop != lines:
        raise ValueError("timeline must cover exactly the configured lines")
    if any(start < 0 or count < 1 or power < 0 for start, count, power in cfg.injections):
        raise ValueError("invalid optical pulse")


def load_scenario(path: Path) -> LoadedScenario:
    original = path.read_bytes()
    # Round-trip enforces finite JSON also inside arbitrary override dictionaries.
    raw = json.loads(original, object_pairs_hook=_pairs)
    canonical = json.dumps(raw, allow_nan=False)
    spec = ScenarioInput.model_validate_json(canonical, strict=True)
    cfg = _patch(nominal_config() if spec.preset == "nominal" else demo_config(), spec.chain)
    if spec.scenes is not None:
        scenes = {}
        for name, row in spec.scenes.items():
            if not name:
                raise ValueError("empty scene name")
            if row.density is not None:
                array = np.asarray(row.density, dtype=float)
                if array.ndim != 2 or array.size == 0 or np.any(array < 0):
                    raise ValueError("density must be a nonempty nonnegative 2D array")
                array = 10.0**-array
            else:
                source = path.parent / row.transmittance_npy
                payload = source.read_bytes()
                if hashlib.sha256(payload).hexdigest() != row.sha256:
                    raise ValueError("scene SHA mismatch")
                array = np.load(io.BytesIO(payload), allow_pickle=False)
            if (
                not isinstance(array, np.ndarray)
                or array.ndim != 2
                or not array.size
                or array.dtype.kind not in "fiu"
                or not np.isfinite(array).all()
                or np.any(array < 0)
                or np.any(array > 1)
            ):
                raise ValueError("transmittance must be finite 2D values in 0..1")
            scenes[name] = Scene(
                np.array(array, dtype=float, copy=True), row.width_m, row.height_m, row.off_film_T
            )
        cfg = replace(cfg, scenes=scenes)
    if spec.timeline is not None:
        allowed = {f.name for f in fields(Phase)}
        if any(set(p) - allowed for p in spec.timeline):
            raise ValueError("unknown timeline field")
        cfg = replace(
            cfg,
            timeline=TypeAdapter(list[Phase]).validate_json(json.dumps(spec.timeline), strict=True),
        )
    cfg.evidence = {
        "all": "assumed",
        "preset": spec.preset,
        "research_scenario_sha256": hashlib.sha256(original).hexdigest(),
    }
    _validate(cfg, spec.lines)
    secondary = _patch(cfg, spec.secondary) if spec.secondary is not None else None
    if secondary is not None:
        _validate(secondary, spec.lines)
        if (
            secondary.fs_hz != cfg.fs_hz
            or secondary.os != cfg.os
            or secondary.domain_id != cfg.domain_id
            or secondary.scan != cfg.scan
            or secondary.adc != cfg.adc
            or (cfg.scan.row_jitter_s and secondary.seed != cfg.seed)
        ):
            raise ValueError("dual channels require identical clocks, scan and ADC encoding")
    return LoadedScenario(spec, original, cfg, secondary)


def scenario_budget(loaded: LoadedScenario) -> dict:
    """Nominal primary-channel geometry/DC projection; no queue/hardware defaults."""
    cfg = loaded.primary
    sc, so, co, de, af, ad = cfg.scan, cfg.source, cfg.collect, cfg.detector, cfg.afe, cfg.adc
    values = {
        "scan.facet_count": sc.facets,
        "scan.rpm": sc.rpm,
        "scan.line_rate_hz": sc.f_line,
        "scan.f_theta_focal_mm": sc.focal_m * 1e3,
        "product.pixel_pitch_um": sc.pitch_x_m * 1e6,
        "budget.pitch_y_um": sc.pitch_y_m * 1e6,
        "budget.spp": cfg.fs_hz * sc.pitch_x_m / sc.v_x,
        "adc.actual_conversion_rate_hz": cfg.fs_hz,
        "budget.nx": sc.nx,
        "budget.ny": loaded.document.lines,
        "laser.power_w": so.power_w,
        "budget.pre_film_efficiency": co.eta_pre,
        "collect.efficiency": co.eta_collect,
        "pmt.qe": de.qe,
        "pmt.gain": de.gain,
        "laser.wavelength_nm": de.wavelength_nm,
        "budget.pixel_time_s": sc.pitch_x_m / sc.v_x,
        "budget.excess_noise_factor": de.excess_noise_factor,
        "pmt.linear_anode_current_max_a": de.anode_limit_a,
        "budget.polarity": af.polarity,
        "budget.driver_gain": af.driver_gain,
        "budget.driver_offset_v": af.driver_offset_v,
        "budget.adc_vmin": ad.vmin,
        "budget.adc_vmax": ad.vmax,
        "adc.module_silkscreen_bits": ad.bits,
        "pmt.dark_current_a": de.dark_current_a,
        "budget.log_offset_v": af.v0 if af.kind == "log" else None,
        "budget.log_slope_v_per_decade": af.slope_v_per_dec if af.kind == "log" else None,
        "budget.log_reference_current_a": af.i_ref_a if af.kind == "log" else None,
        "budget.channels": 2 if loaded.secondary else 1,
        "benchmark.width_pixels": sc.nx,
        "benchmark.height_pixels": loaded.document.lines,
    }
    # Clear table values: missing throughput/memory are not inherited hardware defaults.
    from zlscan.evidence.table import load_parameters

    inputs = {k: {"value": None, "evidence": None} for k in load_parameters()}
    inputs.update(
        {
            k: {"value": v, "evidence": "assumed" if v is not None else None}
            for k, v in values.items()
        }
    )
    return dict(
        mode="research",
        inputs=inputs,
        scenario=dict(
            name="versioned research scenario",
            assumed=True,
            note="Nominal polygon geometry and primary-channel DC only; excludes PSF, distortion, "
            "gating, transient/noise dynamics, stray/rolloff corrections and queue timing. "
            "Second-channel optics are not represented by primary-channel photon/signal rows.",
        ),
    )


def persist_scenario(loaded: LoadedScenario, out: Path, run: dict, *, mode: str) -> dict:
    inputs = out / "inputs"
    inputs.mkdir(exist_ok=False)
    (inputs / "scenario.json").write_bytes(loaded.original)
    marker = loaded.provenance()
    marker["requested_mode"] = mode
    marker["line_count_semantics"] = (
        "per reference target" if mode == "calibration" else "physical lines"
    )
    marker["effective_config_hashes"] = run.get("config_hashes", [run.get("config_hash")])
    marker["channels"] = []
    for i, cfg in enumerate([loaded.primary] + ([loaded.secondary] if loaded.secondary else [])):
        relative = f"inputs/resolved_{i}"
        digest = save_config_bundle(out / relative, cfg)
        marker["channels"].append(dict(path=relative, config_sha256=digest, config_hash=cfg.hash()))
    recipe = loaded.document.model_dump(mode="json")

    def expanded(cfg):
        return {
            f.name: asdict(value) if hasattr(value, "__dataclass_fields__") else value
            for f in fields(cfg)
            if f.name not in {"scenes", "timeline", "evidence"}
            for value in [getattr(cfg, f.name)]
        }

    recipe["chain"] = expanded(loaded.primary)
    recipe["secondary"] = expanded(loaded.secondary) if loaded.secondary else None
    recipe["timeline"] = [asdict(p) for p in loaded.primary.timeline] or None
    recipe["scenes"] = {
        name: dict(
            width_m=scene.width_m,
            height_m=scene.height_m,
            off_film_T=scene.off_film_T,
            transmittance_npy=f"resolved_0/scene_{i}.npy",
            sha256=hashlib.sha256((inputs / f"resolved_0/scene_{i}.npy").read_bytes()).hexdigest(),
        )
        for i, (name, scene) in enumerate(loaded.primary.scenes.items())
    }
    resolved = inputs / "resolved_scenario.json"
    resolved.write_text(json.dumps(recipe, indent=2, allow_nan=False) + "\n")
    marker["resolved_scenario_sha256"] = hashlib.sha256(resolved.read_bytes()).hexdigest()
    run["scenario_configuration"] = marker
    (out / "run.json").write_text(json.dumps(run, indent=2, allow_nan=False) + "\n")
    return run
