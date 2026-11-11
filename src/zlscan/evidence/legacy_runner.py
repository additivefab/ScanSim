"""Run the frozen legacy packages without ever touching ``legacy/`` (decision D001).

Each package is copied into a scratch directory and executed there in a subprocess, so the
frozen tree stays identical to ``legacy/MANIFEST.json`` and Python never writes bytecode into it.

The expected values are not typed in here: they are read from the metrics files that the
package authors delivered inside the frozen tree (status ``author_reported``). A rerun that
reproduces them is an ``independent_rerun``.

Development and test infrastructure only; standard library only.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LEGACY_ROOT = Path(__file__).resolve().parents[3] / "legacy"

# Floats must reproduce to this relative tolerance, integers exactly. Reruns on NumPy 2.3.5
# (delivered), 2.4.2 and 2.5.4 agreed bit for bit except one last-digit difference of 7e-18
# (a summation-order effect), so 1e-12 leaves four orders of margin and still catches any
# real change.
FLOAT_REL_TOL = 1e-12

A_DEMO_CONFIG = "config/demo.json"
A_DELIVERED_METRICS = "package_a/results/default/metrics.json"
A_KEYS = (
    "density_rmse_valid_interior",
    "density_mae_valid_interior",
    "invalid_fraction",
    "raw_active_sample_count",
    "adc_clipped_samples",
    "pmt_clipped_substeps",
)

B_CONFIGS = ("ad9268_50m_5x", "demo_log", "demo_linear", "ideal_regression")
B_KEYS = (
    "density_rmse_unblurred",
    "density_rmse_spatially_blurred",
    "transmission_nonpositive",
    "adc_clip_active",
    "pmt_clip_internal",
)
# Package B delivered the ideal-regression metrics only as the captured standard output.
B_DELIVERED = {
    "ad9268_50m_5x": "package_b/examples/ad9268_50m_5x/metrics.json",
    "demo_log": "package_b/examples/demo_log/metrics.json",
    "demo_linear": "package_b/examples/demo_linear/metrics.json",
    "ideal_regression": "package_b/examples/ideal_regression_stdout.json",
}

A_OWN_TESTS = 14
B_OWN_TESTS = 14


@dataclass
class CommandResult:
    argv: list[str]
    cwd: str
    returncode: int
    output: str
    seconds: float

    def as_command(self) -> dict[str, Any]:
        return {
            "argv": self.argv,
            "cwd": self.cwd,
            "returncode": self.returncode,
            "seconds": round(self.seconds, 3),
        }


def stage_package(name: str, destination: Path, legacy_root: Path | None = None) -> Path:
    """Copy ``legacy/<name>`` to ``destination/<name>`` and return the copy."""
    source = (legacy_root or LEGACY_ROOT) / name
    target = Path(destination) / name
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
    return target


def run_python(args: list[str], cwd: Path, timeout: float = 1800) -> CommandResult:
    """Run this interpreter in ``cwd`` with no bytecode writing and a private matplotlib cache."""
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["MPLCONFIGDIR"] = str(Path(cwd) / ".mplconfig")
    argv = [sys.executable, *args]
    start = time.monotonic()
    done = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return CommandResult(
        argv=argv,
        cwd=str(cwd),
        returncode=done.returncode,
        output=done.stdout + done.stderr,
        seconds=time.monotonic() - start,
    )


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def delivered_metrics(relative: str, legacy_root: Path | None = None) -> dict[str, Any]:
    """Metrics the package author delivered (author_reported), from the frozen tree."""
    return read_json((legacy_root or LEGACY_ROOT) / relative)


def run_a_demo(package: Path, out_dir: Path) -> tuple[CommandResult, dict[str, Any]]:
    result = run_python(["run_demo.py", "--config", A_DEMO_CONFIG, "--out", str(out_dir)], package)
    return result, (read_json(out_dir / "metrics.json") if result.returncode == 0 else {})


def run_a_own_tests(package: Path) -> CommandResult:
    return run_python(["-m", "pytest", "-q", "-p", "no:cacheprovider"], package)


def run_b_sim(
    package: Path, config_name: str, out_dir: Path
) -> tuple[CommandResult, dict[str, Any]]:
    result = run_python(
        ["run_sim.py", "--config", f"configs/{config_name}.json", "--output", str(out_dir)],
        package,
    )
    return result, (read_json(out_dir / "metrics.json") if result.returncode == 0 else {})


def run_b_own_tests(package: Path) -> CommandResult:
    return run_python(["-m", "unittest", "discover", "-s", "tests"], package)


def parse_pytest_passed(output: str) -> int | None:
    """Number of passed tests from pytest's summary line, None if there was a failure or no line."""
    if re.search(r"\b\d+ (failed|error)", output):
        return None
    found = re.search(r"\b(\d+) passed\b", output)
    return int(found.group(1)) if found else None


def parse_unittest_ran(output: str) -> int | None:
    """Number of tests from unittest's summary, None unless the run ended in a plain OK."""
    ran = re.search(r"^Ran (\d+) tests? in", output, flags=re.MULTILINE)
    ok = re.search(r"^OK\s*$", output, flags=re.MULTILINE)
    return int(ran.group(1)) if ran and ok else None
