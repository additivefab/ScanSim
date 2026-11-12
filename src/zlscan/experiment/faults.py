"""Fault injection and run-time detectability (S2-T07; docs/dev/06_testing.md section 6).

The test bench knows what it injected; a real run only has what the data path exposes. For each
fault the harness injects it into the digital chain, then runs only checks a run could make from
delivered data: pattern match (needs a known test pattern), line spacing, file integrity,
overflow consistency, the ignored-sync counter and a channel-identity check (needs different
patterns on the two channels). A fault nothing detects is reported as ``not_distinguishable``
with a way to strengthen the setup. Nothing here is a claim about real hardware.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from zlscan.adc import patterns
from zlscan.adc_link.link import LinkConfig, receive, transmit
from zlscan.contracts.time import ClockDomain
from zlscan.digital.acquisition import (
    AcquisitionConfig,
    acquire,
    block_args,
    session_header,
    write_raw,
)
from zlscan.io.integrity import check_file
from zlscan.io.rawstream import RawWriter, read_all

DOMAIN = ClockDomain(id=1, freq_num=50_000_000)
LINE_CYCLES = 200
LINE_TICKS = LINE_CYCLES * 20_000
CFG = AcquisitionConfig(window_start=10, window_length=40)
N_LINES = 10
N_SAMPLES = LINE_CYCLES * (N_LINES + 2)

FAULTS = (
    "wrong_edge", "wrong_bit_order", "wrong_channel", "overflow_misaligned", "test_code_not_exited",
    "missing_line_sync", "extra_line_sync", "reset_interruption", "clock_offset", "buffer_wrap",
    "bit_flip",
)  # fmt: skip

CHECKS = ("pattern", "line_spacing", "file_integrity", "overflow_consistency", "ignored_sync_counter",
          "channel_identity")  # fmt: skip


@dataclass(frozen=True)
class FaultInfo:
    """One row of the detectability table."""

    location: str
    checker: str
    needs: str
    checkpoint: str
    observable_flag: str
    latency: str
    blind_spot: str
    enhancement: str


INFO: dict[str, FaultInfo] = {
    "wrong_edge": FaultInfo("ADC link capture", "pattern check", "known test pattern",
                            "after capture", "codes differ from the pattern", "one line",
                            "unknown real data", "run a ramp or PRBS before real scans"),
    "wrong_bit_order": FaultInfo("pin to bit mapping", "pattern check", "known test pattern",
                                 "after capture", "codes differ from the pattern", "one line",
                                 "constant mid-scale codes", "use a ramp, not a fixed code"),
    "wrong_channel": FaultInfo("channel order", "channel-identity check", "different patterns on the two channels",
                               "after capture", "channel signature mismatch", "one line",
                               "identical stimulus on both channels", "drive the channels with different test codes"),
    "overflow_misaligned": FaultInfo("overflow flag alignment", "overflow consistency", "data near the rails",
                                     "after capture", "flag where no code is at a rail", "first overflow",
                                     "no sample ever reaches a rail", "include rail codes in the test pattern"),
    "test_code_not_exited": FaultInfo("test-pattern control", "pattern check against the expected cut-over",
                                      "known cut-over time", "after the cut-over", "pattern persists",
                                      "one line", "unknown cut-over", "log the pattern-enable state in the stream"),
    "missing_line_sync": FaultInfo("line sync input", "line spacing", "nominal line period",
                                   "line starts", "a gap of two periods", "one line", "none for gaps",
                                   "count syncs in a register"),
    "extra_line_sync": FaultInfo("line sync input", "ignored-sync counter", "the counter is exposed",
                                 "sync synchroniser", "counter > 0", "one line",
                                 "counter not exposed", "expose the ignored-sync counter"),
    "reset_interruption": FaultInfo("control / reset", "file integrity", "epoch flag discipline",
                                    "block header", "epoch change without a flag", "next block",
                                    "reset that keeps the epoch", "force a new epoch on every reset"),
    "clock_offset": FaultInfo("ADC clock", "line spacing", "nominal samples per line",
                              "line starts", "spacing differs from nominal", "one line",
                              "offset below one sample per line", "compare against a reference counter"),
    "buffer_wrap": FaultInfo("buffer", "pattern check", "known test pattern", "after capture",
                             "old data in a new place", "one line",
                             "real data with valid CRC", "stamp each block with a source-side sequence value"),
    "bit_flip": FaultInfo("before packing", "pattern check", "known test pattern", "after capture",
                          "one code differs", "one line",
                          "flip before the CRC with unknown data (a flip after the CRC is caught by the CRC)",
                          "carry a source-side parity or checksum through the whole path"),
}  # fmt: skip


@dataclass
class FaultOutcome:
    fault: str
    injected: bool
    fired: list[str] = field(default_factory=list)
    verdict: str = "not_distinguishable"  # "detected" | "not_distinguishable"
    enhancement: str = ""
    info: FaultInfo | None = None


def _expected_codes(distinct: bool, seed: int = 6) -> dict[int, np.ndarray]:
    a = patterns.ramp(100, 3, N_SAMPLES)
    b = patterns.prbs(seed, N_SAMPLES) if distinct else a.copy()
    return {0: a, 1: b}


def _read_lines(path: Path) -> dict[int, dict[int, np.ndarray]]:
    """line number -> channel -> codes, plus start index via the blocks."""
    read = read_all(path)
    out: dict[int, dict[int, np.ndarray]] = {}
    starts: dict[int, int] = {}
    for b in read.blocks:
        for s in read.segments:
            if s.epoch == b.epoch and s.channels == b.channels:
                lo = b.first_sample_index - s.first_sample_index
                if 0 <= lo and lo + b.sample_count <= len(s.samples):
                    out[b.line_number] = {
                        c: s.samples[lo : lo + b.sample_count, j] for j, c in enumerate(b.channels)
                    }
                    starts[b.line_number] = b.first_sample_index
                    break
    out["starts"] = starts  # type: ignore[assignment]
    return out


def run_case(
    fault: str | None,
    distinct_channels: bool = True,
    test_pattern_known: bool = True,
    rail_in_stimulus: bool = True,
    workdir: Path | None = None,
) -> FaultOutcome:
    if fault is not None and fault not in FAULTS:
        raise ValueError(f"unknown fault {fault!r}")
    expected = _expected_codes(distinct_channels)
    domain = DOMAIN
    if fault == "clock_offset":
        domain = ClockDomain(id=1, freq_num=int(50_000_000 * 1.005))
    codes = {c: v.copy() for c, v in expected.items()}
    if fault == "test_code_not_exited":
        pass  # the source keeps the pattern; the expected stream cuts over at line 6
    if fault == "overflow_misaligned" and rail_in_stimulus:
        codes[0][3 * LINE_CYCLES + 20] = 65535
        expected[0][3 * LINE_CYCLES + 20] = 65535
    if fault == "bit_flip":
        codes[0][3 * LINE_CYCLES + 20] ^= np.uint16(1 << 3)  # inside the third line's window
    src = patterns.make_source(codes, domain)
    tx = LinkConfig(overflow_offset_cycles=2)
    rx = LinkConfig(overflow_offset_cycles=0)
    rx_ofl = 2
    if fault == "wrong_bit_order":
        rx = LinkConfig(bit_map=tuple(reversed(range(16))))
    if fault == "wrong_channel":
        rx = LinkConfig(channel_order=(1, 0))
        tx = LinkConfig(channel_order=(0, 1), overflow_offset_cycles=2)
    if fault == "overflow_misaligned":
        rx_ofl = 0
    got = receive(transmit(src, tx), rx, rx_overflow_offset_cycles=rx_ofl)
    if fault == "wrong_edge":
        got = patterns.make_source(
            {c: np.roll(got.codes[:, j], 1) for j, c in enumerate(got.channels)}, domain
        )  # fmt: skip

    syncs = [7_000 + i * LINE_TICKS for i in range(N_LINES)]
    if fault == "missing_line_sync":
        del syncs[4]
    if fault == "extra_line_sync":
        syncs.append(syncs[3] + 30 * 20_000)
    res = acquire(got, syncs, CFG)
    if fault == "buffer_wrap":
        res.lines[5].samples = res.lines[1].samples.copy()

    tmp = Path(tempfile.mkdtemp(prefix=f"zlscan_fault_{fault or 'none'}_", dir=workdir))
    path = tmp / "case.zlraw"
    if fault == "reset_interruption":
        with RawWriter(path, session_header(got, CFG, 1)) as w:
            for i, line in enumerate(res.lines):
                args = block_args(got, line, first=i == 0)
                if i >= 5:
                    args["epoch"] = 1
                    args["flags"] &= ~0x20  # the epoch changed but nothing says so
                w.write_block(**args)
    else:
        write_raw(path, got, res, CFG)

    fired = _checks(
        path, got, res, expected, rx_ofl, distinct_channels, test_pattern_known, fault, src
    )
    outcome = FaultOutcome(fault or "none", fault is not None, fired)
    outcome.verdict = "detected" if fired else "not_distinguishable"
    if fault:
        outcome.info = INFO[fault]
        outcome.enhancement = INFO[fault].enhancement if not fired else ""
    return outcome


def _checks(path, got, res, expected, rx_ofl, distinct, pattern_known, fault, src) -> list[str]:
    fired: list[str] = []
    lines = _read_lines(path)
    starts = lines.pop("starts")  # type: ignore[arg-type]
    if pattern_known:
        for n, per in lines.items():
            for c, vals in per.items():
                lo = starts[n]
                want = expected[c][lo : lo + len(vals)]
                if fault == "test_code_not_exited" and lo >= 6 * LINE_CYCLES:
                    want = patterns.prbs(99, N_SAMPLES)[lo : lo + len(vals)]  # expected cut-over
                if not np.array_equal(vals, want):
                    fired.append("pattern")
                    break
            if "pattern" in fired:
                break
    ordered = sorted(starts)
    spacing = [starts[b] - starts[a] for a, b in zip(ordered, ordered[1:], strict=False)]
    if spacing and any(s != LINE_CYCLES for s in spacing):
        fired.append("line_spacing")
    if check_file(path).status != "complete":
        fired.append("file_integrity")
    ofl = got.meta.get("overflow")
    if ofl is not None and ofl.any():
        rails = ((got.codes == 0) | (got.codes == 65535)).any(axis=1)
        if (ofl & ~rails).any():
            fired.append("overflow_consistency")
    if res.ignored_syncs:
        fired.append("ignored_sync_counter")
    if distinct and 0 in lines.get(ordered[0], {}) if ordered else False:
        first = ordered[0]
        a, b = lines[first][0], lines[first][1]
        lo = starts[first]
        if np.array_equal(a, expected[1][lo : lo + len(a)]) or np.array_equal(
            b, expected[0][lo : lo + len(b)]
        ):
            fired.append("channel_identity")
    return sorted(set(fired))
