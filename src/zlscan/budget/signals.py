"""Signal-level budget: do the front-end voltages fit the ADC range, and how fine is one code?

Per density D the anode current is ``I_clear * 10**-D + I_dark``; the first-stage (log) voltage is
``V = offset + polarity * slope * log10(I / Iref)`` (polarity +1: more current, higher voltage; -1:
the opposite, as in package B); the voltage at the chip is ``gain * V + offset_drive``. One ADC
code corresponds to ``LSB / (slope * gain)`` of density. Pole time constants become a smear in
pixels: ``tau * pixel_rate``.

Four different nodes exist on the real chain (log board output, second-stage output, module
input, chip differential input) and must not be mixed. Only the first and the last follow from
what is known here; the other two stay ``None`` with a note, because the second stage and the
module's input network are not known.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, fields

from zlscan.contracts.verdict import Verdict

_REQUIRED = (
    "log_offset_v", "log_slope_v_per_decade", "log_reference_current_a", "polarity", "driver_gain",
    "driver_offset_v", "adc_vmin", "adc_vmax", "adc_bits", "clear_current_a", "dark_current_a",
)  # fmt: skip

NODES = ("log_board_output", "second_stage_output", "module_input", "chip_differential_input")


@dataclass(frozen=True)
class SignalInputs:
    log_offset_v: float | None = None
    log_slope_v_per_decade: float | None = None
    log_reference_current_a: float | None = None
    polarity: int | None = None
    driver_gain: float | None = None
    driver_offset_v: float | None = None
    adc_vmin: float | None = None
    adc_vmax: float | None = None
    adc_bits: int | None = None
    clear_current_a: float | None = None
    dark_current_a: float | None = None


@dataclass(frozen=True)
class SignalRow:
    density: float
    current_a: float
    log_board_v: float
    adc_v: float
    in_adc_range: bool
    margin_v: float  # distance to the nearer rail; negative when out of range


@dataclass
class SignalBudget:
    status: Verdict
    rows: list[SignalRow] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    lsb_v: float | None = None
    density_step_per_code: float | None = None
    tail_pixels: float | None = None
    nodes: dict[str, float | None] = field(default_factory=dict)
    node_notes: dict[str, str] = field(default_factory=dict)


def signal_budget(
    inp: SignalInputs,
    densities: list[float],
    pole_hz: list[float] | None = None,
    pixel_rate_hz: float | None = None,
) -> SignalBudget:
    missing = [f.name for f in fields(inp) if getattr(inp, f.name) is None]
    result = SignalBudget(Verdict.UNRESOLVED if missing else Verdict.PASS, missing=missing)
    if missing:
        return result
    if inp.polarity not in (-1, 1):
        raise ValueError("polarity must be +1 or -1")
    if inp.adc_vmax <= inp.adc_vmin or inp.log_slope_v_per_decade <= 0 or inp.driver_gain <= 0:
        raise ValueError("ADC range, slope and driver gain must be positive and ordered")

    result.lsb_v = (inp.adc_vmax - inp.adc_vmin) / 2**inp.adc_bits
    result.density_step_per_code = result.lsb_v / (inp.log_slope_v_per_decade * inp.driver_gain)
    for d in densities:
        current = inp.clear_current_a * 10 ** (-d) + inp.dark_current_a
        v_log = inp.log_offset_v + inp.polarity * inp.log_slope_v_per_decade * math.log10(
            current / inp.log_reference_current_a
        )
        v_adc = inp.driver_gain * v_log + inp.driver_offset_v
        margin = min(v_adc - inp.adc_vmin, inp.adc_vmax - v_adc)
        result.rows.append(SignalRow(d, current, v_log, v_adc, margin >= 0, margin))
    result.nodes = {
        "log_board_output": result.rows[0].log_board_v if result.rows else None,
        "second_stage_output": None,
        "module_input": None,
        "chip_differential_input": result.rows[0].adc_v if result.rows else None,
    }
    result.node_notes = {
        "log_board_output": "first-stage voltage from the log law",
        "second_stage_output": "未决：二级主放的增益与偏置未知，不能由对数板电压推出",
        "module_input": "未决：ADC 模块的输入网络（耦合、衰减、偏置）未知",
        "chip_differential_input": (
            "driver gain and offset applied; valid only if the driver map is the real one"
        ),
    }
    if pole_hz and pixel_rate_hz is not None:
        tau = sum(1 / (2 * math.pi * f) for f in pole_hz)
        result.tail_pixels = tau * pixel_rate_hz
    return result
