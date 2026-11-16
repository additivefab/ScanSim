"""Scan geometry and line timing (S3-T05).

L0: ideal f-theta, ``x`` proportional to time within the active window. L1 adds a cubic
distortion, a per-facet offset and gain, and a random per-line start jitter. Continuous feed makes
``y`` advance with time. The physical line-sync instant is produced here (integer picoseconds)
and is not related to the ADC clock; the digital side samples it with its own synchroniser.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from zlscan.contracts.module import ModuleSpec
from zlscan.engine.rng import CounterRng

SPEC = ModuleSpec(
    name="truth.scan", interface={"t": "s", "x": "m", "y": "m"}, state={},
    equation="x = x0 + v_x (tau - tau_a) (1 + dg_f) + c3 (u^3) + off_f; y = y0 + v_y t; "
    "v_x = 2 f omega; line k starts at k / f_line + jitter_k",
    parameters={"facets": "1", "rpm": "rpm", "focal_m": "m", "pitch_x_m": "m", "pitch_y_m": "m"},
    applicability="unclamped reflecting polygon with f-theta lens; outside-field mapping is "
                  "assumed extrapolation; no galvanometer servo or speed ripple",
    fidelity="L1", validation=["test_scan_geometry", "test_scan_sync_incoherent",
                              "test_physical_coordinate_continues_before_and_after_retained_window"],
)  # fmt: skip

PS = 10**12


@dataclass(frozen=True)
class ScanConfig:
    facets: int
    rpm: float
    focal_m: float
    pitch_x_m: float
    pitch_y_m: float
    nx: int
    x_center_m: float  # where the beam is when the line is centred
    y0_m: float = 0.0
    active_offset_s: float | None = None  # window start inside the line; None = centred
    continuous_feed: bool = True
    distortion_m: float = 0.0  # cubic term amplitude at the window edge
    facet_offset_m: tuple[float, ...] = ()
    facet_gain: tuple[float, ...] = ()
    sync_offset_ps: int = 7_000
    row_jitter_s: float = 0.0
    extra: dict = field(default_factory=dict)
    legacy_clamped_coordinates: bool = False  # reproduce the obsolete V5 clipping bug explicitly
    # Feed motion candidate (spec 13.3), ASSUMED, off by default.
    feed_ripple: float = 0.0  # fractional sinusoidal modulation of the feed speed
    feed_ripple_hz: float = 0.0
    encoder_pitch_m: float | None = None  # feed encoder resolution; None = no encoder

    def __post_init__(self) -> None:
        if not (math.isfinite(self.feed_ripple) and 0 <= self.feed_ripple < 1):
            raise ValueError("feed ripple must be finite in [0, 1)")
        if not math.isfinite(self.feed_ripple_hz) or self.feed_ripple_hz < 0:
            raise ValueError("feed ripple frequency must be finite and non-negative")
        if self.feed_ripple and self.feed_ripple_hz <= 0:
            raise ValueError("feed ripple needs a positive frequency")
        if self.encoder_pitch_m is not None and not (
            math.isfinite(self.encoder_pitch_m) and self.encoder_pitch_m > 0
        ):
            raise ValueError("encoder pitch must be positive and finite or null")

    @property
    def f_line(self) -> float:
        return self.facets * self.rpm / 60

    @property
    def omega(self) -> float:
        return 2 * math.pi * self.rpm / 60

    @property
    def v_x(self) -> float:
        return 2 * self.focal_m * self.omega

    @property
    def v_y(self) -> float:
        return self.pitch_y_m * self.f_line

    @property
    def width_m(self) -> float:
        return self.nx * self.pitch_x_m

    @property
    def active_s(self) -> float:
        return self.width_m / self.v_x

    @property
    def line_period_s(self) -> float:
        return 1 / self.f_line

    def window_start_s(self) -> float:
        if self.active_offset_s is not None:
            return self.active_offset_s
        return (self.line_period_s - self.active_s) / 2


def line_start_s(cfg: ScanConfig, k: np.ndarray, rng: CounterRng, domain: int) -> np.ndarray:
    base = k / cfg.f_line
    if cfg.row_jitter_s:
        uniq = np.unique(k)
        z = {int(u): float(rng.normal("row_jitter", domain, int(u), 1)[0]) for u in uniq}
        base = base + cfg.row_jitter_s * np.array([z[int(u)] for u in k])
    return base


def geometry(cfg: ScanConfig, t_s: np.ndarray, rng: CounterRng, domain: int):
    """Per-step line number, position, facet, and whether the beam is inside the active window."""
    k = np.floor(t_s * cfg.f_line + 1e-12).astype(np.int64)
    k = np.maximum(k, 0)
    tau = t_s - line_start_s(cfg, k, rng, domain)
    ta = cfg.window_start_s()
    inside = (tau >= ta) & (tau < ta + cfg.active_s)
    facet = k % cfg.facets
    u = (tau - ta) / cfg.active_s  # physical trajectory continues outside the active window
    if cfg.legacy_clamped_coordinates:
        u = np.clip(u, 0, 1)
    gain = np.ones_like(u)
    if cfg.facet_gain:
        gain = np.array(cfg.facet_gain)[facet]
    x = cfg.x_center_m - cfg.width_m / 2 + cfg.width_m * u * gain
    if cfg.distortion_m:
        x = x + cfg.distortion_m * (2 * u - 1) ** 3
    if cfg.facet_offset_m:
        x = x + np.array(cfg.facet_offset_m)[facet]
    y = cfg.y0_m + (_feed(cfg, t_s) if cfg.continuous_feed else k * cfg.pitch_y_m)
    return k, x, y, facet, inside


def _feed(cfg: ScanConfig, t_s: np.ndarray) -> np.ndarray:
    """Feed position: v_y t, plus the integral of a sinusoidal speed modulation if enabled."""
    y = cfg.v_y * t_s
    if cfg.feed_ripple:
        w = 2 * math.pi * cfg.feed_ripple_hz
        y = y + cfg.v_y * cfg.feed_ripple * (1 - np.cos(w * t_s)) / w
    return y


def encoder_counts(cfg: ScanConfig, t_s: np.ndarray) -> np.ndarray | None:
    """Observable feed-encoder count at times ``t_s``: the true feed position, quantised.

    The instrument's view of the feed, not the nominal ``v_y * t``; ``None`` without encoder.
    """
    if cfg.encoder_pitch_m is None:
        return None
    t = np.asarray(t_s, float)
    return np.floor((cfg.y0_m + _feed(cfg, t)) / cfg.encoder_pitch_m + 1e-9)


def sync_ticks(cfg: ScanConfig, lines: int, rng: CounterRng, domain: int) -> list[int]:
    """Physical line-sync instants in integer picoseconds (not aligned to any ADC edge)."""
    k = np.arange(lines)
    t = line_start_s(cfg, k, rng, domain)
    return [int(round(float(x) * PS)) + cfg.sync_offset_ps for x in t]
