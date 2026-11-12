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
    """Compile and run the HDL; compare sample, line, code, window flags and missed syncs."""
    compiler = iverilog or shutil.which("iverilog")
    runner = vvp or shutil.which("vvp")
    if compiler is None or runner is None:
        raise RuntimeError("Icarus Verilog iverilog and vvp are required")
    count = 64
    patterns = {
        "ramp": ramp(65520, 7, count),
        "alternating": alternating(0x5555, 0xAAAA, count),
        "prbs": prbs(12, count),
    }
    cases = {}
    with tempfile.TemporaryDirectory(prefix="zlscan_cosim_") as directory:
        work = Path(directory)
        executable = work / "capture.vvp"
        subprocess.run(
            [
                compiler,
                "-g2012",
                "-s",
                "tb_scanner_capture",
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
                tuple(map(int, line.split()[1:])) for line in output if line.startswith("CAPTURE ")
            ]
            ignored = [int(line.split()[1]) for line in output if line.startswith("IGNORED ")]
            source = ConvertedSamples(np.asarray(codes).reshape(-1, 1), (0,), 0, 0, _CLOCK, 16)
            reference = acquire(source, _SYNCS, _CONFIG)
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
            }
    return {
        "mode": "cosim",
        "simulator": compiler,
        "cases": cases,
        "passed": all(case["match"] for case in cases.values()),
    }
