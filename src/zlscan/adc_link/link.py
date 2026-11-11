"""ADC output link, functional level (S2-T02).

Source code -> output coding -> pin bit order -> bus/channel order -> pipeline latency ->
output propagation -> capture edge. Each delay is counted exactly once and appears as its own
timestamp: ``sample_time``, ``adc_output_time`` (pipeline), ``pin_arrival_time``
(propagation), ``capture_time`` (the next capture edge). The receiver undoes the mapping with
its *own* configuration; a receiver that disagrees with the transmitter recovers wrong codes,
which is how wiring and configuration faults become visible.

This is not a timing analysis of real pins.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from zlscan.adc.patterns import ConvertedSamples
from zlscan.contracts.time import ClockDomain


@dataclass(frozen=True)
class LinkConfig:
    bits: int = 16
    coding: str = "offset_binary"  # "offset_binary" | "twos_complement"
    bit_map: tuple[int, ...] | None = None  # pin p carries source bit bit_map[p]; None = identity
    channel_order: tuple[int, ...] | None = None  # bus j carries channel channel_order[j]
    capture_edge: str = "rising"  # "rising" | "falling"
    dco_phase_ps: int = 0
    latency_half_cycles: int | None = None  # 24 = 12 cycles, 25 = 12.5; None = unknown
    propagation_ps: int | None = None
    overflow_offset_cycles: int = 0
    register_readback: bool | None = None  # was the effective configuration read back?

    def resolved_bit_map(self) -> tuple[int, ...]:
        return self.bit_map if self.bit_map is not None else tuple(range(self.bits))


@dataclass
class LinkOutput:
    words: np.ndarray  # (n, channels) pin words, bus order
    overflow: np.ndarray  # (n,) bool, already shifted by the transmitter's offset
    sample_time: np.ndarray
    adc_output_time: np.ndarray
    pin_arrival_time: np.ndarray
    capture_time: np.ndarray
    first_index: int
    epoch: int
    channels: tuple[int, ...]  # channel on each bus, bus order
    domain: ClockDomain
    config: LinkConfig = field(default_factory=LinkConfig)


class LinkConfigError(ValueError):
    pass


def check_config(cfg: LinkConfig, mode: str = "research") -> None:
    if sorted(cfg.resolved_bit_map()) != list(range(cfg.bits)):
        raise LinkConfigError("bit_map must be a permutation of the data bits")
    if cfg.coding not in ("offset_binary", "twos_complement"):
        raise LinkConfigError(f"unknown coding {cfg.coding!r}")
    if mode == "hardware_constrained":
        missing = []
        if cfg.latency_half_cycles is None:
            missing.append("latency_half_cycles")
        if cfg.propagation_ps is None:
            missing.append("propagation_ps")
        if cfg.register_readback is not True:
            missing.append("register_readback (effective configuration not read back)")
        if missing:
            raise LinkConfigError("hardware_constrained: missing " + ", ".join(missing))


def _permute_bits(values: np.ndarray, bit_map: tuple[int, ...], inverse: bool) -> np.ndarray:
    out = np.zeros_like(values, dtype=np.uint32)
    src = values.astype(np.uint32)
    for pin, bit in enumerate(bit_map):
        if inverse:  # pin word -> source code
            out |= ((src >> pin) & 1) << bit
        else:  # source code -> pin word
            out |= ((src >> bit) & 1) << pin
    return out.astype(np.uint16)


def _apply_coding(values: np.ndarray, coding: str, bits: int) -> np.ndarray:
    if coding == "twos_complement":  # offset binary <-> two's complement flips the sign bit
        return values ^ np.uint16(1 << (bits - 1))
    return values


def transmit(src: ConvertedSamples, cfg: LinkConfig, mode: str = "research") -> LinkOutput:
    check_config(cfg, mode)
    if src.bits != cfg.bits:
        raise LinkConfigError("source and link disagree on the number of bits")
    channels = cfg.channel_order or src.channels
    if sorted(channels) != sorted(src.channels):
        raise LinkConfigError("channel_order must name exactly the source channels")
    columns = [src.channels.index(c) for c in channels]
    codes = src.codes[:, columns]
    words = _permute_bits(_apply_coding(codes, cfg.coding, cfg.bits), cfg.resolved_bit_map(), False)

    n = len(src)
    top = 2**cfg.bits - 1
    saturated = ((src.codes == 0) | (src.codes == top)).any(axis=1)
    shift = cfg.overflow_offset_cycles
    overflow = np.zeros(n, dtype=bool)
    if shift >= 0 and n > shift:
        overflow[shift:] = saturated[: n - shift]
    elif shift < 0 and n > -shift:
        overflow[:shift] = saturated[-shift:]

    d = src.domain
    t_sample = src.sample_times()
    half = np.int64((d._numerator * (cfg.latency_half_cycles or 0)) // (2 * d.freq_num))
    t_out = t_sample + half
    t_pin = t_out + np.int64(cfg.propagation_ps or 0)
    period2 = d._numerator / d.freq_num / 2  # half period in ps (float only for the grid origin)
    grid = _capture_grid(t_pin, d, cfg, period2)
    return LinkOutput(words, overflow, t_sample, t_out, t_pin, grid, src.first_index,
                      src.epoch, tuple(channels), d, cfg)  # fmt: skip


def _capture_grid(t_pin: np.ndarray, d: ClockDomain, cfg: LinkConfig, half: float) -> np.ndarray:
    """First capture edge at or after the data arrives (integer arithmetic on the clock grid)."""
    offset = cfg.dco_phase_ps + (round(half) if cfg.capture_edge == "falling" else 0)
    out = np.empty_like(t_pin)
    for i, t in enumerate(t_pin):
        base = int(t) - offset - d.phase_ticks
        k = -((-base * d.freq_num) // d._numerator)  # ceil(base / period)
        out[i] = d.tick_of(k) + offset
    return out


def receive(
    out: LinkOutput, rx: LinkConfig, rx_overflow_offset_cycles: int = 0
) -> ConvertedSamples:
    """Recover the codes with the receiver's own configuration."""
    check_config(rx)
    codes = _apply_coding(_permute_bits(out.words, rx.resolved_bit_map(), True), rx.coding, rx.bits)
    rx_channels = rx.channel_order or out.channels
    if sorted(rx_channels) != sorted(out.channels):
        raise LinkConfigError("receiver channel_order disagrees about which channels exist")
    # the receiver's bus j is assumed to carry channel rx_channels[j]
    order = sorted(range(len(rx_channels)), key=lambda j: rx_channels[j])
    codes = codes[:, order]
    channels = tuple(sorted(rx_channels))
    ofl = out.overflow
    if rx_overflow_offset_cycles:
        ofl = np.roll(ofl, -rx_overflow_offset_cycles)
    return ConvertedSamples(
        codes.astype(np.uint16), channels, out.first_index, out.epoch, out.domain, rx.bits,
        meta={"overflow": ofl, "capture_time": out.capture_time},
    )  # fmt: skip
