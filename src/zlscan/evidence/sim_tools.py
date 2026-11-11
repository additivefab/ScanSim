"""Run the frozen legacy RTL testbenches (Icarus Verilog) and SPICE netlists (ngspice).

Scope (decision D001, docs/dev/stages/stage0.md S0-T04): execute what the packages shipped,
record the outcome, and never claim more. Icarus runs a behavioural testbench; a ``PASS:`` line
means that testbench's own assertions held, not that an FPGA design is verified. ngspice runs
behavioural netlists; where the netlist's own comment states an analytic behaviour, that is
checked against the closed-form curve.

Helpers here are pure (parsing, reference curves) so they are unit-testable without the tools.
"""

from __future__ import annotations

import math
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from zlscan.evidence.legacy_runner import run_command
from zlscan.evidence.records import write_record
from zlscan.io.manifest import sha256_file

_VERSION_ARGS = {"iverilog": ["-V"], "vvp": ["-V"], "ngspice": ["--version"]}
_ERROR_LINE = re.compile(r"^(error|fatal)\b|watchdog timeout", re.IGNORECASE)


@dataclass(frozen=True)
class ToolInfo:
    name: str
    path: str
    version: str


@dataclass(frozen=True)
class SimCase:
    """One shipped testbench or netlist. ``files`` are relative to ``legacy/``."""

    name: str
    kind: str  # "rtl" | "spice" | "vivado"
    tools: tuple[str, ...]
    files: tuple[str, ...]
    expected_pass_lines: int | None = None  # rtl only: PASS: lines the testbench must print
    note: str = ""


CASES: tuple[SimCase, ...] = (
    SimCase(
        "package_a_rtl_icarus",
        "rtl",
        ("iverilog", "vvp"),
        (
            "package_a/rtl/scanner_capture.sv",
            "package_a/rtl/tb_scanner_capture.sv",
            "package_a/rtl/vectors.hex",
            "package_a/rtl/run_iverilog.sh",
        ),
        expected_pass_lines=2,
    ),
    SimCase(
        "package_b_rtl_icarus",
        "rtl",
        ("iverilog", "vvp"),
        (
            "package_b/rtl/tag_delay.sv",
            "package_b/rtl/adc_stream_fifo.sv",
            "package_b/rtl/tb_scanner.sv",
            "package_b/rtl/run_iverilog.sh",
        ),
        expected_pass_lines=1,
    ),
    SimCase(
        "package_a_spice_log_frontend",
        "spice",
        ("ngspice",),
        ("package_a/spice/log_frontend_ngspice.cir",),
    ),
    SimCase(
        "package_a_spice_ac_coupling",
        "spice",
        ("ngspice",),
        ("package_a/spice/ac_coupling_risk_ngspice.cir",),
        note="its comment states an RC high-pass behaviour that can be checked analytically",
    ),
    SimCase(
        "package_b_spice_behavioral",
        "spice",
        ("ngspice",),
        ("package_b/spice/behavioral_log_frontend.cir",),
    ),
    SimCase(
        "package_a_spice_ltspice",
        "spice",
        ("LTspice",),
        ("package_a/spice/log_frontend_ltspice.cir",),
        note="LTspice-specific netlist; ngspice is not a substitute",
    ),
    SimCase(
        "packages_ab_rtl_vivado_xsim",
        "vivado",
        ("xvlog", "xelab", "xsim"),
        (
            "package_a/rtl/run_xsim.sh",
            "package_a/rtl/run.tcl",
            "package_b/rtl/run_xsim.tcl",
        ),
        note="Vivado is not part of this project's tooling",
    ),
)


def missing_tools(case: SimCase, found: dict[str, ToolInfo | None]) -> list[str]:
    """Names of the tools a case needs that are not in ``found`` (or found as None)."""
    return [t for t in case.tools if found.get(t) is None]


def write_not_executed_records(
    runs_dir: Path,
    date: str,
    repo_root: Path,
    found: dict[str, ToolInfo | None],
    code: dict,
    environment: dict,
    now: datetime,
) -> list[Path]:
    """Write one ``not_executed`` record per case, none of which claims a result.

    Refuses (writing nothing) when some case has all its tools: the run path is not implemented
    because it cannot be written or verified without the real tools, and a stub would only
    produce records that look like results.
    """
    runnable = [c.name for c in CASES if not missing_tools(c, found)]
    if runnable:
        raise RuntimeError(
            "tools are present for " + ", ".join(runnable) + ", but the run path is not "
            "implemented yet; implement and verify it before recording results"
        )
    probed = sorted({t for c in CASES for t in c.tools})
    log = "".join(
        f"{tool}: {'not found' if found.get(tool) is None else found[tool].path} (PATH probe)\n"
        for tool in probed
    )
    paths = []
    for case in CASES:
        missing = missing_tools(case, found)
        notes = (
            f"Not executed: required tool(s) not found on PATH: {', '.join(missing)}. "
            f"{case.note + '. ' if case.note else ''}"
            "See docs/reports/simulator_install_attempt.md for the installation attempt."
        )
        paths.append(
            write_record(
                Path(runs_dir) / f"{date}_sim_{case.name}",
                name=f"sim_{case.name}",
                status="independent_rerun",
                conclusion="not_executed",
                commands=[],
                inputs={
                    f"legacy/{f}": sha256_file(Path(repo_root) / "legacy" / f) for f in case.files
                },
                seed=None,
                criteria=[],
                started_at=now,
                finished_at=datetime.now(UTC),
                log_text=log,
                code=code,
                environment=environment,
                notes=notes,
            )
        )
    return paths


def find_tool(name: str) -> ToolInfo | None:
    """Locate an executable on PATH and read its version banner; None if it is not installed."""
    path = shutil.which(name)
    if path is None:
        return None
    result = run_command([path, *_VERSION_ARGS.get(name, ["--version"])], Path.cwd(), timeout=60)
    lines = [line.strip() for line in result.output.splitlines() if line.strip()]
    return ToolInfo(name=name, path=path, version=lines[0] if lines else "unknown")


def parse_vvp(output: str) -> dict[str, list[str]]:
    """Split vvp output into the testbench's PASS lines and any error/fatal/timeout lines."""
    pass_lines: list[str] = []
    error_lines: list[str] = []
    for raw in output.splitlines():
        line = raw.strip()
        if line.startswith("PASS:"):
            pass_lines.append(line)
        elif _ERROR_LINE.search(line):
            error_lines.append(line)
    return {"pass_lines": pass_lines, "error_lines": error_lines}


def read_wrdata(path: Path) -> tuple[list[str] | None, np.ndarray]:
    """Read an ngspice ``wrdata`` file: optional header of vector names, then numeric rows."""
    names: list[str] | None = None
    rows: list[list[float]] = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        parts = raw.split()
        if not parts:
            continue
        try:
            rows.append([float(p) for p in parts])
        except ValueError:
            if rows:
                raise ValueError(f"non-numeric line after data in {path}: {raw!r}") from None
            names = parts
    if not rows:
        raise ValueError(f"no numeric rows in {path}")
    if len({len(r) for r in rows}) != 1:
        raise ValueError(f"rows in {path} have different numbers of columns")
    return names, np.array(rows)


def value_at(t: np.ndarray, v: np.ndarray, at: float) -> float:
    return float(np.interp(at, t, v))


def rc_highpass_step(t: float, t0: float, step: float, tau: float) -> float:
    """Output of a series-C / shunt-R coupling after a voltage step of ``step`` at ``t0``."""
    return 0.0 if t < t0 else step * math.exp(-(t - t0) / tau)


def log_stage_voltage(current: float, v_off: float, slope: float, i_ref: float) -> float:
    """Static output of the legacy behavioural log stage: V = v_off + slope * log10(i_ref / I)."""
    return v_off + slope * math.log10(i_ref / current)
