"""Build a machine-readable current snapshot of the stage 3-7 evidence gates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "evidence_runs/2026-11-14_stage3_7_snapshot/record/snapshot.json"

INPUTS = {
    "stage34_software": ROOT / "evidence_runs/2026-11-13_stage34_software_gate/acceptance.json",
    "stage7_residuals": ROOT / "evidence_runs/2026-11-14_stage7_residual_audit/record/audit.json",
    "ad9268_documents": ROOT / "evidence_runs/2026-11-14_ad9268_document_audit/record/audit.json",
    "ci": ROOT / "evidence_runs/2026-11-14_cross_platform_ci/record/37e4e12_acceptance.json",
}


def _load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def audit(output: Path = DEFAULT_OUTPUT) -> dict:
    evidence = {name: _load(path) for name, path in INPUTS.items()}
    performance = evidence["stage34_software"].get("performance", {})
    ci_jobs = evidence["ci"].get("jobs", [])
    unresolved = [
        "W07 page-level paired off-film illumination scenario",
        "M02 board register readback, test-pattern capture, measured clocks and channel mapping",
        "M06 point-target/knife-edge/grid measurements",
        "measured device residuals and independent timing traces",
        "complete external tool/license lock",
    ]
    result = {
        "format": "zlscan-stage3-7-snapshot/1",
        "status": "partial",
        "scope": (
            "current software/evidence state; unresolved hardware and measured gates "
            "are not inferred"
        ),
        "evidence": {
            "stage34_software_status": evidence["stage34_software"].get("status"),
            "stage7_residual_status": evidence["stage7_residuals"].get("status"),
            "ad9268_document_files": len(
                evidence["ad9268_documents"].get("documents_manifest", {}).get("files", [])
            ),
            "ad9268_sqlite_integrity": evidence["ad9268_documents"]
            .get("project", {})
            .get("sqlite_integrity"),
            "ci_status": evidence["ci"].get("conclusion") or evidence["ci"].get("status"),
            "ci_job_count": len(ci_jobs),
            "ci_success_jobs": sum(job.get("conclusion") == "success" for job in ci_jobs),
            "performance_wall_s": {
                case: values.get("wall_s") for case, values in performance.items()
            },
        },
        "unresolved": unresolved,
        "inputs": {name: str(path.relative_to(ROOT)) for name, path in INPUTS.items()},
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
