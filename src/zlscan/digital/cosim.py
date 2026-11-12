"""Compare real Icarus output against the observable digital acquisition model."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from zlscan.adc.patterns import ConvertedSamples, alternating, prbs, ramp
from zlscan.contracts.time import ClockDomain
from zlscan.digital.acquisition import AcquisitionConfig, acquire

RTL = Path(__file__).resolve().parents[3] / "cosim" / "rtl"
_CLOCK = ClockDomain(1, 1_000_000_000, phase_ticks=500)
_SYNCS = [25, 1550, 15000, 26030]
_CONFIG = AcquisitionConfig(2, 5)


def run_capture_cosim(iverilog: str | None = None, vvp: str | None = None) -> dict:
    """Check source identities and window flags, including crossing the old 32-bit boundary."""
    compiler = iverilog or shutil.which("iverilog")
    runner = vvp or shutil.which("vvp")
    if compiler is None or runner is None:
        raise RuntimeError("Icarus Verilog iverilog and vvp are required")
    patterns = {
        "ramp": ramp(65520, 7, 64),
        "alternating": alternating(0x5555, 0xAAAA, 64),
        "prbs": prbs(12, 64),
    }
    groups = {}
    with tempfile.TemporaryDirectory(prefix="zlscan_cosim_") as directory:
        work = Path(directory)
        for group, start_index in (("normal", 0), ("cross_32bit", (1 << 32) - 16)):
            executable = work / f"{group}.vvp"
            subprocess.run(
                [
                    compiler,
                    "-g2012",
                    "-s",
                    "tb_scanner_capture",
                    f"-Ptb_scanner_capture.SAMPLE_START=64'd{start_index}",
                    "-o",
                    str(executable),
                    str(RTL / "scanner_capture.sv"),
                    str(RTL / "tb_scanner_capture.sv"),
                ],
                cwd=work,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            cases = {}
            for name, codes in patterns.items():
                (work / "codes.hex").write_text(
                    "".join(f"{int(code):04x}\n" for code in codes), encoding="ascii"
                )
                output = subprocess.run(
                    [runner, str(executable)],
                    cwd=work,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=30,
                ).stdout.splitlines()
                actual = [
                    tuple(map(int, line.split()[1:]))
                    for line in output
                    if line.startswith("CAPTURE ")
                ]
                ignored = [int(line.split()[1]) for line in output if line.startswith("IGNORED ")]
                source = ConvertedSamples(
                    np.asarray(codes).reshape(-1, 1), (0,), start_index, 0, _CLOCK, 16
                )
                shift = _CLOCK.tick_of(start_index) - _CLOCK.tick_of(0)
                reference = acquire(source, [tick + shift for tick in _SYNCS], _CONFIG)
                expected = [
                    (
                        line.start_index + index,
                        line.line_number,
                        int(code),
                        int(index == 0),
                        int(index == len(line.samples) - 1),
                    )
                    for line in reference.lines
                    for index, code in enumerate(line.samples[:, 0])
                ]
                cases[name] = {
                    "samples": len(actual),
                    "ignored_syncs": ignored,
                    "expected_ignored_syncs": len(reference.ignored_syncs),
                    "match": actual == expected and ignored == [len(reference.ignored_syncs)],
                    "records": actual,
                }
            groups[group] = cases
        reset_executable = work / "counter_reset.vvp"
        subprocess.run(
            [
                compiler,
                "-g2012",
                "-s",
                "tb_counter_reset",
                "-o",
                str(reset_executable),
                str(RTL / "scanner_capture.sv"),
                str(RTL / "tb_counter_reset.sv"),
            ],
            cwd=work,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        reset_output = subprocess.run(
            [runner, str(reset_executable)],
            cwd=work,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout.splitlines()
        reset_records = [
            tuple(map(int, line.split()[1:]))
            for line in reset_output
            if line.startswith("RESET_CAPTURE ")
        ]
        reset_passed = "RESET_DURING_BUSY" in reset_output and "RESET_OK" in reset_output
    return {
        "mode": "cosim",
        "simulator": compiler,
        "cases": groups["normal"],
        "counter_boundary_cases": groups["cross_32bit"],
        "sample_index_bits": 64,
        "counter_reset": {"passed": reset_passed, "records": reset_records},
        "passed": reset_passed
        and all(case["match"] for group in groups.values() for case in group.values()),
    }
