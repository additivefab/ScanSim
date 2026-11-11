"""Scan geometry as coupled constraints (docs/dev/04_physics_spec.md, section 2).

Facets, rotation speed, focal length, pixel pitch, sampling rate, samples per pixel and the
active duty are not independent: for example 6 facets at 10000 rpm with a 200 mm focal length and
50 um pixels give 8.378 Mpixel/s, not 10. ``solve`` takes whichever quantities the caller knows,
derives the rest by propagating the relations below, and reports a conflict (naming the quantity
and both values) when an extra input disagrees with what the others imply.

Every derived value is labelled ``derived``, lists the quantities it came from, and carries the
weakest evidence of the inputs it depends on; an input with no stated evidence counts as
``assumed``. Values are never invented: what cannot be derived stays unresolved.

SI units throughout: metres, seconds, hertz, radians; ``rpm`` is revolutions per minute.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from zlscan.contracts.param import Evidence

TWO_PI = 2 * math.pi
RELATIVE_TOLERANCE = 1e-6

VARIABLES = (
    "facets", "rpm", "omega_rad_s", "f_line_hz", "line_period_s", "focal_m", "theta_max_rad",
    "width_m", "height_m", "nx", "ny", "duty", "active_time_s", "v_x_m_s", "v_y_m_s",
    "pitch_x_m", "pitch_y_m", "pixel_rate_hz", "fs_hz", "spp", "frame_time_s",
)  # fmt: skip

# Weakest first. Derived quantities inherit the minimum of what they depend on.
_STRENGTH = {
    Evidence.ASSUMED: 0, Evidence.PROJECT_TEXT: 1, Evidence.PHOTO: 2, Evidence.DERIVED: 2,
    Evidence.SCHEMATIC: 3, Evidence.DATASHEET: 3, Evidence.MEASURED: 4,
}  # fmt: skip


@dataclass(frozen=True)
class Rule:
    name: str
    target: str
    inputs: tuple[str, ...]
    fn: Callable[..., float]


def _rules() -> list[Rule]:
    r: list[Rule] = []

    def add(name: str, target: str, inputs: tuple[str, ...], fn: Callable[..., float]) -> None:
        r.append(Rule(name, target, inputs, fn))

    # line rate = facets * rpm / 60
    add("f_line = facets*rpm/60", "f_line_hz", ("facets", "rpm"), lambda n, rpm: n * rpm / 60)
    add("f_line = facets*rpm/60", "rpm", ("f_line_hz", "facets"), lambda f, n: 60 * f / n)
    add("f_line = facets*rpm/60", "facets", ("f_line_hz", "rpm"), lambda f, rpm: 60 * f / rpm)
    # omega = 2 pi rpm / 60
    add("omega = 2*pi*rpm/60", "omega_rad_s", ("rpm",), lambda rpm: TWO_PI * rpm / 60)
    add("omega = 2*pi*rpm/60", "rpm", ("omega_rad_s",), lambda w: 60 * w / TWO_PI)
    # scan speed v_x = 2 f omega (a reflecting mirror turns the beam by twice its own angle)
    add("v_x = 2*focal*omega", "v_x_m_s", ("focal_m", "omega_rad_s"), lambda f, w: 2 * f * w)
    add("v_x = 2*focal*omega", "focal_m", ("v_x_m_s", "omega_rad_s"), lambda v, w: v / (2 * w))
    add("v_x = 2*focal*omega", "omega_rad_s", ("v_x_m_s", "focal_m"), lambda v, f: v / (2 * f))
    # pixel rate = v_x / pitch_x
    add("pixel_rate = v_x/pitch_x", "pixel_rate_hz", ("v_x_m_s", "pitch_x_m"), lambda v, p: v / p)
    add("pixel_rate = v_x/pitch_x", "v_x_m_s", ("pixel_rate_hz", "pitch_x_m"), lambda q, p: q * p)
    add("pixel_rate = v_x/pitch_x", "pitch_x_m", ("v_x_m_s", "pixel_rate_hz"), lambda v, q: v / q)
    # ADC rate = samples per pixel * pixel rate
    add("fs = spp*pixel_rate", "fs_hz", ("spp", "pixel_rate_hz"), lambda m, q: m * q)
    add("fs = spp*pixel_rate", "pixel_rate_hz", ("fs_hz", "spp"), lambda fs, m: fs / m)
    add("fs = spp*pixel_rate", "spp", ("fs_hz", "pixel_rate_hz"), lambda fs, q: fs / q)
    # f-theta: width = 2 f theta_max
    add(
        "width = 2*focal*theta_max", "width_m", ("focal_m", "theta_max_rad"), lambda f, t: 2 * f * t
    )
    add(
        "width = 2*focal*theta_max",
        "theta_max_rad",
        ("width_m", "focal_m"),
        lambda w, f: w / (2 * f),
    )
    add(
        "width = 2*focal*theta_max",
        "focal_m",
        ("width_m", "theta_max_rad"),
        lambda w, t: w / (2 * t),
    )
    # duty: each facet sweeps 4 pi / facets of optical angle, the film uses 2 theta_max of it
    add(
        "duty = facets*theta_max/(2*pi)",
        "duty",
        ("facets", "theta_max_rad"),
        lambda n, t: n * t / TWO_PI,
    )
    add(
        "duty = facets*theta_max/(2*pi)",
        "theta_max_rad",
        ("duty", "facets"),
        lambda d, n: TWO_PI * d / n,
    )
    add(
        "duty = facets*theta_max/(2*pi)",
        "facets",
        ("duty", "theta_max_rad"),
        lambda d, t: TWO_PI * d / t,
    )
    # line period and active time
    add("line_period = 1/f_line", "line_period_s", ("f_line_hz",), lambda f: 1 / f)
    add("line_period = 1/f_line", "f_line_hz", ("line_period_s",), lambda t: 1 / t)
    add("active = duty*line_period", "active_time_s", ("duty", "line_period_s"), lambda d, t: d * t)
    add("active = duty*line_period", "duty", ("active_time_s", "line_period_s"), lambda a, t: a / t)
    add("active = duty*line_period", "line_period_s", ("active_time_s", "duty"), lambda a, d: a / d)
    # the pixels of one line fill the active time: nx * spp / fs
    add(
        "active = nx*spp/fs", "active_time_s", ("nx", "spp", "fs_hz"), lambda nx, m, fs: nx * m / fs
    )
    add("active = nx*spp/fs", "nx", ("active_time_s", "spp", "fs_hz"), lambda a, m, fs: a * fs / m)
    # pixel counts, feed and frame time
    add("nx = width/pitch_x", "nx", ("width_m", "pitch_x_m"), lambda w, p: w / p)
    add("nx = width/pitch_x", "width_m", ("nx", "pitch_x_m"), lambda n, p: n * p)
    add("nx = width/pitch_x", "pitch_x_m", ("width_m", "nx"), lambda w, n: w / n)
    add("ny = height/pitch_y", "ny", ("height_m", "pitch_y_m"), lambda h, p: h / p)
    add("ny = height/pitch_y", "height_m", ("ny", "pitch_y_m"), lambda n, p: n * p)
    add("ny = height/pitch_y", "pitch_y_m", ("height_m", "ny"), lambda h, n: h / n)
    add("v_y = pitch_y*f_line", "v_y_m_s", ("pitch_y_m", "f_line_hz"), lambda p, f: p * f)
    add("v_y = pitch_y*f_line", "pitch_y_m", ("v_y_m_s", "f_line_hz"), lambda v, f: v / f)
    add("v_y = pitch_y*f_line", "f_line_hz", ("v_y_m_s", "pitch_y_m"), lambda v, p: v / p)
    add("frame = ny*line_period", "frame_time_s", ("ny", "line_period_s"), lambda n, t: n * t)
    add("frame = ny*line_period", "ny", ("frame_time_s", "line_period_s"), lambda f, t: f / t)
    add("frame = ny*line_period", "line_period_s", ("frame_time_s", "ny"), lambda f, n: f / n)
    return r


RULES = _rules()


@dataclass(frozen=True)
class Provenance:
    kind: str  # "given" | "derived"
    rule: str | None = None
    inputs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Conflict:
    variable: str
    given: float
    derived: float
    rule: str
    inputs: tuple[str, ...]
    roots: tuple[str, ...]  # the caller's own inputs that imply the derived value
    message: str


@dataclass
class SolveResult:
    values: dict[str, float] = field(default_factory=dict)
    provenance: dict[str, Provenance] = field(default_factory=dict)
    weakest_evidence: dict[str, Evidence] = field(default_factory=dict)
    conflicts: list[Conflict] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    @property
    def consistent(self) -> bool:
        return not self.conflicts


def _roots(names: tuple[str, ...], provenance: Mapping[str, Provenance]) -> tuple[str, ...]:
    """The inputs the caller actually supplied that these quantities ultimately come from."""
    found: set[str] = set()
    stack = list(names)
    while stack:
        name = stack.pop()
        prov = provenance[name]
        if prov.kind == "given":
            found.add(name)
        else:
            stack.extend(prov.inputs)
    return tuple(sorted(found))


def _weakest(*items: Evidence) -> Evidence:
    return min(items, key=lambda e: _STRENGTH[e])


def solve(
    given: Mapping[str, float], evidence: Mapping[str, Evidence] | None = None
) -> SolveResult:
    for name, value in given.items():
        if name not in VARIABLES:
            raise ValueError(f"unknown geometry variable {name!r}")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"{name} must be a number")
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be positive and finite, got {value!r}")
    evidence = evidence or {}
    result = SolveResult()
    for name, value in given.items():
        result.values[name] = float(value)
        result.provenance[name] = Provenance("given")
        result.weakest_evidence[name] = evidence.get(name) or Evidence.ASSUMED

    changed = True
    while changed:
        changed = False
        for rule in RULES:
            if not all(i in result.values for i in rule.inputs):
                continue
            derived = rule.fn(*(result.values[i] for i in rule.inputs))
            if rule.target not in result.values:
                result.values[rule.target] = derived
                result.provenance[rule.target] = Provenance("derived", rule.name, rule.inputs)
                result.weakest_evidence[rule.target] = _weakest(
                    *(result.weakest_evidence[i] for i in rule.inputs)
                )
                changed = True
            else:
                have = result.values[rule.target]
                if not math.isclose(have, derived, rel_tol=RELATIVE_TOLERANCE):
                    if not any(
                        c.variable == rule.target and c.rule == rule.name for c in result.conflicts
                    ):
                        roots = _roots(rule.inputs, result.provenance)
                        message = (
                            f"{rule.target} = {have:.9g} disagrees with {derived:.9g} implied by "
                            f"{', '.join(rule.inputs)} ({rule.name}); these follow from your "
                            f"inputs {', '.join(roots)}"
                        )
                        result.conflicts.append(
                            Conflict(
                                rule.target, have, derived, rule.name, rule.inputs, roots, message
                            )
                        )
    result.unresolved = [v for v in VARIABLES if v not in result.values]
    return result
