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
from zlscan.contracts.time import CaptureClock, ClockDomain


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
    run_id: str = "run"
    branch_id: str = "main"
    session_id: int = 1
    overflow_valid: np.ndarray | None = None


class LinkConfigError(ValueError):
    pass


def check_config(cfg: LinkConfig, mode: str = "research") -> None:
    if type(cfg.bits) is not int or not 1 <= cfg.bits <= 16:
        raise LinkConfigError("link bits must be an integer in 1..16")
    if cfg.capture_edge not in {"rising", "falling"}:
        raise LinkConfigError("capture_edge must be rising or falling")
    for name in ("latency_half_cycles", "propagation_ps"):
        value = getattr(cfg, name)
        if value is not None and (type(value) is not int or value < 0):
            raise LinkConfigError(f"{name} must be a nonnegative integer or unknown")
    if type(cfg.overflow_offset_cycles) is not int or type(cfg.dco_phase_ps) is not int:
        raise LinkConfigError("overflow offset and DCO phase must be integers")
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

    valid = np.zeros(n, dtype=bool)
    if shift >= 0:
        valid[min(n, shift) :] = True
    else:
        valid[: max(0, n + shift)] = True

    d = src.domain
    t_sample = src.sample_times()
    t_out = _pipeline_times(t_sample, d, src.first_index, cfg.latency_half_cycles or 0)
    t_pin = _shift_times(t_out, cfg.propagation_ps or 0)
    grid = _capture_grid(t_pin, d, cfg)
    return LinkOutput(words, overflow, t_sample, t_out, t_pin, grid, src.first_index,
                      src.epoch, tuple(channels), d, cfg,
                      src.run_id, src.branch_id, src.session_id, valid)  # fmt: skip


def _shift_times(times: np.ndarray, delay: int) -> np.ndarray:
    bounds = np.iinfo(np.int64)
    if not bounds.min <= delay <= bounds.max or (
        len(times)
        and (int(times.min()) + delay < bounds.min or int(times.max()) + delay > bounds.max)
    ):
        raise LinkConfigError("link timestamp exceeds signed 64-bit Tick range")
    return times + np.int64(delay)


def _pipeline_times(
    times: np.ndarray, domain: ClockDomain, first: int, half_cycles: int
) -> np.ndarray:
    delay, remainder = divmod(domain._numerator * half_cycles, 2 * domain.freq_num)
    output = _shift_times(times, delay)
    if remainder:
        # Round the complete edge once; adding two rounded intervals can be one ps early.
        correction = np.fromiter(
            (
                (2 * ((first + i) * domain._numerator % domain.freq_num) + remainder)
                // (2 * domain.freq_num)
                for i in range(len(times))
            ),
            dtype=np.int64,
            count=len(times),
        )
        if np.any((output == np.iinfo(np.int64).max) & (correction != 0)):
            raise LinkConfigError("link timestamp exceeds signed 64-bit Tick range")
        output += correction
    return output


def _capture_grid(t_pin: np.ndarray, d: ClockDomain, cfg: LinkConfig) -> np.ndarray:
    """First exact rational rising/falling edge at or after arrival, rounded only once."""
    clock = CaptureClock(d, edge=cfg.capture_edge, phase_ticks=cfg.dco_phase_ps)
    out = np.empty_like(t_pin)
    bounds = np.iinfo(np.int64)
    for i, t in enumerate(t_pin):
        tick = clock.tick_of(clock.first_at(int(t)))
        if not bounds.min <= tick <= bounds.max:
            raise LinkConfigError("capture timestamp exceeds signed 64-bit Tick range")
        out[i] = tick
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
    if type(rx_overflow_offset_cycles) is not int:
        raise LinkConfigError("receiver overflow offset must be an integer")
    ofl = out.overflow.copy()
    valid = out.overflow_valid.copy() if out.overflow_valid is not None else np.ones(len(ofl), bool)
    shift = rx_overflow_offset_cycles
    if shift:
        ofl, valid = np.zeros_like(ofl), np.zeros_like(valid)
        start, stop = max(0, -shift), min(len(ofl), len(ofl) - shift)
        if stop > start:
            ofl[start:stop] = out.overflow[start + shift : stop + shift]
            source_valid = (
                out.overflow_valid if out.overflow_valid is not None else np.ones(len(ofl), bool)
            )
            valid[start:stop] = source_valid[start + shift : stop + shift]
    return ConvertedSamples(
        codes.astype(np.uint16), channels, out.first_index, out.epoch, out.domain, rx.bits,
        run_id=out.run_id, branch_id=out.branch_id, session_id=out.session_id,
        meta={"overflow": ofl, "overflow_valid": valid, "capture_time": out.capture_time},
    )  # fmt: skip
