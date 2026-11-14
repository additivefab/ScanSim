"""Audit replayable stage-7 residual evidence without upgrading it to measured data."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "docs/reports/legacy_cross_compare_data.json"
PAIR = ROOT / "docs/reports/stage4_package_b_data.json"
DEFAULT_OUTPUT = ROOT / "evidence_runs/2026-11-14_stage7_residual_audit/record/audit.json"


def _load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _criterion(name: str, passed: bool, detail: str) -> dict:
    return {"name": name, "passed": bool(passed), "detail": detail}


def audit(output: Path = DEFAULT_OUTPUT) -> dict:
    legacy = _load(LEGACY)
    rows = legacy.get("rows", [])
    static_ok = bool(rows) and all(
        row.get("a", {}).get("code") == row.get("b", {}).get("code") for row in rows
    )
    max_abs = {
        key: max(abs(float(row.get("absolute_difference", {}).get(key, math.inf))) for row in rows)
        for key in ("power_w", "anode_a", "v_log", "v_adc", "code")
    }

    pair = _load(PAIR)
    dynamic_ok = True
    dynamic_rows = {}
    for name, data in pair.items():
        noise = data.get("variants", {}).get("noise", {})
        uncorrected = float(noise["uncorrected"]["mse"])
        aligned = float(noise["edge_calibrated"]["mse"])
        shift = float(data["edge_calibrated_shift_px"])
        valid = data.get("shared_pixels", 0) > 0 and math.isfinite(shift) and aligned <= uncorrected
        dynamic_ok &= valid
        dynamic_rows[name] = {
            "shared_pixels": data["shared_pixels"],
            "edge_calibrated_shift_px": shift,
            "uncorrected_mse": uncorrected,
            "edge_calibrated_mse": aligned,
            "software_candidate_pass": valid,
        }

    criteria = [
        _criterion(
            "static chain residuals",
            static_ok,
            f"rows={len(rows)}; max_abs={max_abs}",
        ),
        _criterion(
            "dynamic alignment residuals",
            dynamic_ok and len(dynamic_rows) == 2,
            f"cases={len(dynamic_rows)}; aligned MSE does not exceed uncorrected MSE",
        ),
        _criterion(
            "measured device residuals",
            False,
            "no measured device capture, independent timing record, or instrument "
            "trace was supplied",
        ),
    ]
    result = {
        "format": "zlscan-stage7-residual-audit/1",
        "scope": (
            "software and frozen-package replay only; measured device acceptance "
            "is required separately"
        ),
        "status": "pass_with_scope" if all(item["passed"] for item in criteria[:2]) else "fail",
        "criteria": criteria,
        "static": {"rows": len(rows), "max_absolute_difference": max_abs},
        "dynamic": dynamic_rows,
        "inputs": {"legacy": str(LEGACY.relative_to(ROOT)), "pair": str(PAIR.relative_to(ROOT))},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = audit(args.output)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["status"] == "pass_with_scope" else 1


if __name__ == "__main__":
    raise SystemExit(main())
