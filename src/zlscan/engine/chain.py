"""The forward physical chain, advanced by absolute fine steps (S3-T01, S3-T12).

laser -> scan -> film (T) -> spot -> collection -> PMT -> analogue front end -> ADC.

Everything is a function of the absolute fine-step index. Line number, beam position and the
random draws come from it, so advancing in one go or in any chunking gives the same ADC codes
(bitwise). The only carried state is the front-end filter delay lines and the last fine voltage;
it is what a checkpoint stores. Line starts reset nothing.

Probes are read-only: they copy arrays out after the fact and never touch the generator or the
state, so switching them on or off changes no code.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field

import numpy as np

from zlscan.contracts.time import ClockDomain
from zlscan.engine.rng import CounterRng
from zlscan.truth import adc as adc_mod
from zlscan.truth import afe as afe_mod
from zlscan.truth import collect as collect_mod
from zlscan.truth import detector as det_mod
from zlscan.truth import psf as psf_mod
from zlscan.truth import scan as scan_mod
from zlscan.truth import source as src_mod
from zlscan.truth.scene import Scene

MODEL_VERSION = "chain-0.1"
PROBE_NAMES = ("power_w", "t_eff", "current_a", "v_adc", "x_m", "line")


@dataclass(frozen=True)
class Phase:
    """A stretch of the experiment timeline, in whole lines."""

    name: str
    first_line: int
    n_lines: int
    scene: str
    laser_on: bool = True


@dataclass
class ChainConfig:
    seed: int
    fs_hz: int
    os: int
    scan: scan_mod.ScanConfig
    source: src_mod.SourceConfig
    collect: collect_mod.CollectConfig
    psf: psf_mod.PsfConfig
    detector: det_mod.DetectorConfig
    afe: afe_mod.AfeConfig
    adc: adc_mod.AdcConfig
    scenes: dict[str, Scene]
    timeline: list[Phase] = field(default_factory=list)
    domain_id: int = 1
    evidence: dict[str, str] = field(default_factory=dict)
    # Legacy behaviours, explicit and off by default (only the legacy presets turn them on).
    legacy_reset_per_line: bool = False

    def hash(self) -> str:
        d = asdict(self)
        scenes = d.pop("scenes")
        d["scenes"] = {
            k: hashlib.sha256(np.ascontiguousarray(v["transmittance_map"]).tobytes()).hexdigest()
            + f":{v['width_m']}:{v['height_m']}:{v['off_film_T']}"
            for k, v in scenes.items()
        }
        return hashlib.sha256(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()


@dataclass
class ChunkOut:
    first_sample: int
    codes: np.ndarray
    saturated: np.ndarray
    front_end_clipped: np.ndarray
    over_limit: np.ndarray
    probes: dict[str, np.ndarray] = field(default_factory=dict)


class Chain:
    def __init__(self, cfg: ChainConfig) -> None:
        if cfg.os < 2 or cfg.os % 2:
            raise ValueError("os must be an even integer >= 2")
        if cfg.detector.pole_hz is not None:
            raise NotImplementedError("the PMT bandwidth pole is declared but not implemented")
        self.cfg = cfg
        self.dt = 1.0 / (cfg.fs_hz * cfg.os)
        self.rng = CounterRng(cfg.seed)
        self.afe = afe_mod.Afe(cfg.afe, self.dt)
        self.fine_clock = ClockDomain(cfg.domain_id, cfg.fs_hz * cfg.os)
        self.adc_clock = ClockDomain(cfg.domain_id, cfg.fs_hz)
        self.step = 0
        self.last_v: float | None = None
        self.probe_names: set[str] = set()
        self.observers: list[Callable[[str, int, np.ndarray], None]] = []

    # ---- timeline -----------------------------------------------------------------------
    def _phase(self, line: int) -> Phase:
        tl = self.cfg.timeline
        if not tl:
            return Phase("scene", 0, 1 << 60, next(iter(self.cfg.scenes)))
        for ph in tl:
            if ph.first_line <= line < ph.first_line + ph.n_lines:
                return ph
        return tl[-1] if line >= tl[-1].first_line else tl[0]

    # ---- one chunk ----------------------------------------------------------------------
    def advance(self, n: int) -> ChunkOut:
        if not self.cfg.legacy_reset_per_line:
            return self._advance(n)
        per_line = round(self.cfg.scan.line_period_s * self.cfg.fs_hz * self.cfg.os)
        parts: list[ChunkOut] = []
        left = n
        while left:
            into = self.step % per_line
            if into == 0:  # legacy: every line starts from a fresh front-end state
                self.afe.reset()
                self.last_v = None
            take = min(left, per_line - into)
            parts.append(self._advance(take))
            left -= take
        return merge(parts)

    def _advance(self, n: int) -> ChunkOut:
        cfg, s0 = self.cfg, self.step
        t = (s0 + np.arange(n)) * self.dt
        k, x, y, facet, inside = scan_mod.geometry(cfg.scan, t, self.rng, cfg.domain_id)

        t_eff = np.ones(n)
        laser_on = np.ones(n, dtype=bool)
        for line in np.unique(k):
            m = k == line
            ph = self._phase(int(line))
            laser_on[m] = ph.laser_on
            if ph.laser_on:
                scene = cfg.scenes[ph.scene]
                t_eff[m] = psf_mod.effective_transmittance(scene.lookup, cfg.psf, x[m], y[m])
        u = np.clip((x - (cfg.scan.x_center_m - cfg.scan.width_m / 2)) / cfg.scan.width_m, 0, 1)
        p0 = src_mod.power(cfg.source, t, k, self.rng, cfg.domain_id, inside, laser_on)
        eta = collect_mod.efficiency(cfg.collect, u)
        lit = laser_on & (inside if cfg.source.gated else True)
        power = p0 * eta * t_eff + np.where(lit, cfg.collect.stray_w, 0.0)

        current, over = det_mod.anode_current(cfg.detector, power, self.dt, self.rng,
                                              cfg.domain_id, s0)  # fmt: skip
        v, clipped = self.afe.process(current)

        # ADC samples sit on the fine steps that are multiples of os
        first_sample = -(-s0 // cfg.os)
        last_sample = (s0 + n - 1) // cfg.os
        idx = np.arange(first_sample, last_sample + 1) * cfg.os - s0
        v_prev = np.concatenate([[self.last_v if self.last_v is not None else v[0]], v[:-1]])
        vs = v[idx]
        if cfg.adc.jitter_rms_s:
            jit = cfg.adc.jitter_rms_s * self.rng.normal(
                "adc_jitter", cfg.domain_id, first_sample, len(idx)
            )
            vs = adc_mod.sample_with_jitter(vs, v_prev[idx], self.dt, jit)
        codes, sat = adc_mod.quantize(cfg.adc, vs)

        out = ChunkOut(first_sample, codes, sat, clipped[idx], over[idx])
        values = {"power_w": power, "t_eff": t_eff, "current_a": current, "v_adc": v,
                  "x_m": x, "line": k}  # fmt: skip
        for name in self.probe_names:
            out.probes[name] = values[name].copy()
            for obs in self.observers:
                obs(name, s0, out.probes[name])
        self.last_v = float(v[-1])
        self.step = s0 + n
        return out

    def run_steps(self, total: int, chunk: int) -> ChunkOut:
        parts, done = [], 0
        while done < total:
            m = min(chunk, total - done)
            parts.append(self.advance(m))
            done += m
        return merge(parts)

    # ---- checkpoint ---------------------------------------------------------------------
    def checkpoint(self) -> bytes:
        return json.dumps(
            {
                "model_version": MODEL_VERSION,
                "config_hash": self.cfg.hash(),
                "step": self.step,
                "last_v": self.last_v,
                "afe": self.afe.state(),
            }
        ).encode()

    def restore(self, blob: bytes) -> None:
        d = json.loads(blob)
        if d["model_version"] != MODEL_VERSION:
            raise ValueError(f"checkpoint of model {d['model_version']}, running {MODEL_VERSION}")
        if d["config_hash"] != self.cfg.hash():
            raise ValueError("checkpoint was written under a different configuration")
        self.step, self.last_v = d["step"], d["last_v"]
        self.afe.set_state(d["afe"])


def merge(parts: list[ChunkOut]) -> ChunkOut:
    probes = {}
    for name in parts[0].probes:
        probes[name] = np.concatenate([p.probes[name] for p in parts])
    return ChunkOut(
        parts[0].first_sample,
        np.concatenate([p.codes for p in parts]),
        np.concatenate([p.saturated for p in parts]),
        np.concatenate([p.front_end_clipped for p in parts]),
        np.concatenate([p.over_limit for p in parts]),
        probes,
    )
