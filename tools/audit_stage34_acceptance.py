"""Validate the current reproducible software evidence for stages 3 and 4.

The report deliberately keeps the scope to replayable software artifacts.  A successful
result is not a hardware, browser, or measured-source acceptance verdict.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "evidence_runs/2026-11-13_stage34_software_gate/acceptance.json"
PERFORMANCE_ROOT = ROOT / "evidence_runs/2026-11-13_stage3_optimized_performance"
FACTORIAL = ROOT / "docs/reports/stage4_factorial_data.json"
PAIR = ROOT / "docs/reports/stage4_package_b_data.json"
NOMINAL = ROOT / "docs/reports/nominal_quality_metrics.json"


def _load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _criterion(name: str, passed: bool, detail: str) -> dict:
    return {"name": name, "passed": bool(passed), "detail": detail}


def audit(output: Path = DEFAULT_OUTPUT) -> dict:
    criteria: list[dict] = []
    performance = {}
    for case in ("4000x5000", "7000x8600"):
        data = _load(PERFORMANCE_ROOT / case / "output/performance.json")
        performance[case] = {
            key: data[key]
            for key in (
                "lines",
                "fine_steps_per_channel",
                "integrity",
                "recon_status",
                "lost_blocks",
                "kernel_s",
                "wall_s",
                "peak_rss_bytes",
                "assumed_parameters",
            )
        }
        criteria.append(
            _criterion(
                f"performance:{case}",
                data["integrity"] == "complete"
                and data["recon_status"] == "complete"
                and data["lost_blocks"] == 0
                and data["wall_s"] <= 900.0,
                "wall_s={}; integrity={}; lost_blocks={}".format(
                    data["wall_s"], data["integrity"], data["lost_blocks"]
                ),
            )
        )

    factorial = _load(FACTORIAL)
    cases = factorial.get("cases", {})
    invalid_total = 0
    for case in cases.values():
        invalid_total += sum(
            (case.get("comparison", {}).get("invalid_reason_counts", {}) or {}).values()
        )
    criteria.append(
        _criterion(
            "factorial:2^3 cases",
            factorial.get("mode") == "stage4_factorial"
            and factorial.get("contains_truth") is True
            and len(cases) == 8
            and factorial.get("common_pixels") == 288
            and invalid_total == 0,
            "cases={}; common_pixels={}; invalid_total={}".format(
                len(cases), factorial.get("common_pixels"), invalid_total
            ),
        )
    )

    pair = _load(PAIR)
    pair_ok = True
    pair_details = []
    for name, data in pair.items():
        shift = data.get("edge_calibrated_shift_px")
        variants = data.get("variants", {})
        uncorrected = variants.get("noise", {}).get("uncorrected", {}).get("mse")
        corrected = variants.get("noise", {}).get("edge_calibrated", {}).get("mse")
        valid = (
            isinstance(shift, (int, float))
            and math.isfinite(shift)
            and data.get("shared_pixels", 0) > 0
            and isinstance(uncorrected, (int, float))
            and isinstance(corrected, (int, float))
            and corrected <= uncorrected
        )
        pair_ok &= valid
        pair_details.append(f"{name}: shift={shift}; uncorrected={uncorrected}; edge={corrected}")
    criteria.append(
        _criterion(
            "pair:independent edge alignment", pair_ok and len(pair) == 2, "; ".join(pair_details)
        )
    )

    nominal = _load(NOMINAL)
    criteria.append(
        _criterion(
            "nominal:quality summary",
            nominal.get("valid_pixels", 0) > 0 and bool(nominal.get("wedge", {}).get("steps")),
            "valid_pixels={}; wedge_steps={}".format(
                nominal.get("valid_pixels"), len(nominal.get("wedge", {}).get("steps", []))
            ),
        )
    )

    result = {
        "format": "zlscan-stage34-software-gate/1",
        "scope": (
            "replayable software evidence only; no browser, hardware, or measured-source acceptance"
        ),
        "status": "pass" if all(item["passed"] for item in criteria) else "fail",
        "criteria": criteria,
        "performance": performance,
        "inputs": {
            "factorial": str(FACTORIAL.relative_to(ROOT)),
            "pair": str(PAIR.relative_to(ROOT)),
            "nominal": str(NOMINAL.relative_to(ROOT)),
        },
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
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
