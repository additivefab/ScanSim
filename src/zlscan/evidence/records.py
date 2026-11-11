"""Evidence records for formal runs (docs/dev/06_testing.md, section 7).

A record says what was run, on which code and inputs, in which environment, against which
criteria, and what came out. The writer refuses to produce a record that overstates a result:

* ``pass`` is refused when any criterion failed or any command returned non-zero;
* an ``independent_rerun`` that claims a result (``pass`` or ``fail``) is refused unless the
  commands, input hashes, code commit and environment are all present (an incompletely
  documented rerun is only "author_reported");
* an existing record directory is never overwritten.

Standard library only.
"""

from __future__ import annotations

import json
import math
import platform
import subprocess
from collections.abc import Mapping, Sequence
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import Any

FORMAT = "zlscan-evidence/1"
STATUSES = ("author_reported", "independent_rerun", "measured")
CONCLUSIONS = ("pass", "fail", "not_executed", "not_obtained")

_TRACKED_PACKAGES = ("numpy", "scipy", "matplotlib", "tifffile", "pillow", "pydantic", "h5py")


def compare(
    quantity: str,
    expected: Any,
    actual: Any,
    *,
    abs_tol: float = 0.0,
    rel_tol: float = 0.0,
) -> dict[str, Any]:
    """One acceptance criterion: what was expected, the tolerance, what was found, and the verdict.

    Integers, strings and booleans are compared exactly. Floats pass when
    ``|actual - expected| <= abs_tol + rel_tol * |expected|``; with both tolerances zero they
    must be equal. NaN never passes.
    """
    if expected is None or isinstance(expected, (bool, int, str)):
        return {
            "quantity": quantity,
            "expected": expected,
            "tolerance": None,
            "actual": actual,
            "passed": actual == expected,
        }
    finite = math.isfinite(expected) and isinstance(actual, (int, float)) and math.isfinite(actual)
    passed = finite and abs(actual - expected) <= abs_tol + rel_tol * abs(expected)
    return {
        "quantity": quantity,
        "expected": expected,
        "tolerance": {"abs": abs_tol, "rel": rel_tol},
        "actual": actual,
        "passed": bool(passed),
    }


def environment_info() -> dict[str, Any]:
    packages = {}
    for name in _TRACKED_PACKAGES:
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "packages": packages,
    }


def git_state(repo: Path) -> dict[str, Any]:
    """Commit hash and whether the working tree has uncommitted changes (None if unknown)."""
    unknown = {"commit": None, "dirty": None}
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return unknown
    return {"commit": commit, "dirty": bool(status.strip())}


def _check(status: str, conclusion: str) -> None:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}, got {status!r}")
    if conclusion not in CONCLUSIONS:
        raise ValueError(f"conclusion must be one of {CONCLUSIONS}, got {conclusion!r}")


def _missing_for_independent_rerun(
    commands: Sequence[Mapping[str, Any]],
    inputs: Mapping[str, str],
    code: Mapping[str, Any] | None,
    environment: Mapping[str, Any] | None,
) -> list[str]:
    missing = []
    if not commands:
        missing.append("commands")
    if not inputs:
        missing.append("input hashes")
    if not code or not code.get("commit"):
        missing.append("code commit")
    if not environment:
        missing.append("environment")
    return missing


def write_record(
    directory: Path,
    *,
    name: str,
    status: str,
    conclusion: str,
    commands: Sequence[Mapping[str, Any]],
    inputs: Mapping[str, str],
    seed: Any,
    criteria: Sequence[Mapping[str, Any]],
    started_at: datetime,
    finished_at: datetime,
    log_text: str,
    code: Mapping[str, Any] | None,
    environment: Mapping[str, Any] | None,
    notes: str = "",
) -> Path:
    """Write ``directory/record.json`` and ``directory/log.txt``; return the record path."""
    _check(status, conclusion)
    # Completeness is demanded only when a result is claimed; "not_executed" and
    # "not_obtained" claim nothing, so they need no commands or hashes.
    if status == "independent_rerun" and conclusion in ("pass", "fail"):
        missing = _missing_for_independent_rerun(commands, inputs, code, environment)
        if missing:
            raise ValueError(
                "an independent_rerun record needs a complete record; missing: "
                + ", ".join(missing)
            )
    if conclusion == "pass":
        if any(not c["passed"] for c in criteria):
            raise ValueError("conclusion 'pass' refused: at least one criterion failed")
        if any(c["returncode"] != 0 for c in commands):
            raise ValueError("conclusion 'pass' refused: at least one command returned non-zero")

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    record = {
        "format": FORMAT,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "code": dict(code) if code else None,
        "environment": dict(environment) if environment else None,
        "seed": seed,
        "inputs": dict(inputs),
        "commands": [dict(c) for c in commands],
        "criteria": [dict(c) for c in criteria],
        "notes": notes,
    }
    path = directory / "record.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (directory / "log.txt").write_text(log_text, encoding="utf-8")
    return path
