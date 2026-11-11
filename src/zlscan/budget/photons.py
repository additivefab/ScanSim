"""Photon budget: photoelectrons per pixel at the PMT, and what they do to density noise.

Formulas (docs/dev/04_physics_spec.md, section 10):

    P_clear = laser * eta_pre * eta_collect            power on the PMT through clear film
    rate    = P * lambda / (h c) * QE                   primary photoelectrons per second
    N_e     = rate * t_pixel * 10**(-D)
    I_anode = e * G * rate * 10**(-D)
    sigma_D = sqrt(F / N_e) / ln 10                     shot noise only, high-count limit

``sigma_D`` holds only for N_e much larger than one; it is returned together with a flag and the
flag is False below ``HIGH_COUNT_THRESHOLD`` (a convention, not a derivation). Nothing is ever
filled in: an unknown input (``None``) yields no number, only the list of what is missing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, fields

from zlscan.contracts.verdict import Verdict

H = 6.62607015e-34
C = 299792458.0
E = 1.602176634e-19
HIGH_COUNT_THRESHOLD = 100.0

_REQUIRED = (
    "laser_w", "pre_film_efficiency", "collection_efficiency", "qe", "gain", "wavelength_nm",
    "pixel_time_s",
)  # fmt: skip
_POSITIVE = (*_REQUIRED, "excess_noise_factor", "anode_limit_a")


@dataclass(frozen=True)
class PhotonInputs:
    laser_w: float | None = None
    pre_film_efficiency: float | None = None
    collection_efficiency: float | None = None
    qe: float | None = None
    gain: float | None = None
    wavelength_nm: float | None = None
    pixel_time_s: float | None = None
    excess_noise_factor: float | None = None
    anode_limit_a: float | None = None


@dataclass(frozen=True)
class PhotonRow:
    density: float
    electrons_per_pixel: float
    anode_current_a: float
    over_limit: bool | None  # None when no anode limit is known
    sigma_density: float | None  # None when the excess-noise factor is unknown
    high_count_valid: bool | None


@dataclass
class PhotonBudget:
    status: Verdict
    rows: list[PhotonRow] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    partial_missing: list[str] = field(default_factory=list)
    electrons_needed: float | None = None


def sigma_density(electrons: float, excess_noise_factor: float) -> float:
    if electrons <= 0 or excess_noise_factor < 1:
        raise ValueError("electrons must be positive and the excess-noise factor at least 1")
    return math.sqrt(excess_noise_factor / electrons) / math.log(10)


def electrons_needed(target_sigma_density: float, excess_noise_factor: float) -> float:
    if target_sigma_density <= 0:
        raise ValueError("the target noise must be positive")
    return excess_noise_factor / (target_sigma_density * math.log(10)) ** 2


def photon_budget(
    inp: PhotonInputs, densities: list[float], target_sigma_density: float | None = None
) -> PhotonBudget:
    for f in fields(inp):
        value = getattr(inp, f.name)
        if value is not None and f.name in _POSITIVE and not (math.isfinite(value) and value > 0):
            raise ValueError(f"{f.name} must be positive and finite")
    missing = [name for name in _REQUIRED if getattr(inp, name) is None]
    if missing:
        return PhotonBudget(Verdict.UNRESOLVED, missing=missing)

    p_clear = inp.laser_w * inp.pre_film_efficiency * inp.collection_efficiency
    photon_energy = H * C / (inp.wavelength_nm * 1e-9)
    rate = p_clear / photon_energy * inp.qe
    result = PhotonBudget(Verdict.PASS)
    if inp.excess_noise_factor is None:
        result.partial_missing.append("excess_noise_factor")
    if inp.anode_limit_a is None:
        result.partial_missing.append("anode_limit_a")
    for d in densities:
        n_e = rate * inp.pixel_time_s * 10 ** (-d)
        current = E * inp.gain * rate * 10 ** (-d)
        sigma = (
            None if inp.excess_noise_factor is None else sigma_density(n_e, inp.excess_noise_factor)
        )
        result.rows.append(
            PhotonRow(
                d,
                n_e,
                current,
                None if inp.anode_limit_a is None else current > inp.anode_limit_a,
                sigma,
                None if sigma is None else n_e >= HIGH_COUNT_THRESHOLD,
            )  # fmt: skip
        )
    if target_sigma_density is not None and inp.excess_noise_factor is not None:
        result.electrons_needed = electrons_needed(target_sigma_density, inp.excess_noise_factor)
    return result
