"""One command that reruns every stage 3/4 software gate and writes a single report.

Steps: ruff, the not-slow pytest suite, the archived-evidence gate (``audit_stage34_acceptance``),
then criteria over the N1/N2 evidence kept in ``evidence_runs/2026-11-16_*/record``. Items that
need a browser, hardware or measured data are listed as ``not_executed``; they are never passed.

    .venv/bin/python tools/run_stage34_gates.py --out evidence_runs/DIR/gates.json [--no-tests]

Exit code 0 only when no software criterion failed. ``partial`` and ``not_executed`` do not
make the exit code non-zero, and they are never rolled up into "pass".
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import audit_stage34_acceptance as archived  # noqa: E402

LOW_LIGHT = ROOT / "evidence_runs/2026-11-16_low_light_protocol2/record/summary.json"
ZL80_METRICS = ROOT / "evidence_runs/2026-11-16_zl80_baseline/record/metrics.json"
W07 = ROOT / "evidence_runs/2026-11-16_browser_w07/record/w07.json"
ZL80_LOG = ROOT / "evidence_runs/2026-11-16_zl80_baseline/record/full_frame.log"

NOT_EXECUTED = [
    ("browser:real-drag-frame-rate", "mouse-drag frame rate on a real browser"),
    ("browser:full-trace-visual", "full-length trace visual detail"),
    ("browser:cross-epoch-imaging", "cross-epoch imaging page"),
    ("hardware:M02-M08", "board/instrument measurements; none obtained"),
    ("rtl:icarus-cosim", "Icarus Verilog unavailable on this host"),
]


def run(cmd: list[str]) -> dict:
    start = time.time()
    done = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)
    tail = (done.stdout + done.stderr).strip().splitlines()[-3:]
    return {"returncode": done.returncode, "seconds": round(time.time() - start, 1), "tail": tail}


def criterion(name: str, status: str, detail: str) -> dict:
    return {"name": name, "status": status, "detail": detail}


def low_light() -> dict:
    d = json.loads(LOW_LIGHT.read_text(encoding="utf-8"))
    n = d["comparison_status_counts"]
    unchanged = d["raw_inputs_unchanged"] and d["disjoint_seeds"]
    if n.get("outside_equivalence_band") or not unchanged:
        status = "fail"
    elif n.get("inconclusive"):
        status = "partial"
    else:
        status = "pass"
    return criterion("n2:low-light-protocol-2", status, json.dumps(n, sort_keys=True))


def zl80() -> list[dict]:
    m = json.loads(ZL80_METRICS.read_text(encoding="utf-8"))
    worst = 0.0
    for ch in m["channels"]:
        for row in ch["steps"]:
            if row["std_od"] is not None and row["budget_high_count_valid"]:
                worst = max(worst, abs(row["std_od"] / row["budget_shot_sigma_od"] - 1))
    noise = criterion(
        "n1:zl80-noise-vs-budget",
        "pass" if worst <= 0.05 else "fail",
        f"worst relative std error {worst:.3f} over high-count steps (limit 0.05)",
    )
    log = ZL80_LOG.read_text(encoding="utf-8")
    real = re.search(r"([\d.]+)\s+real", log)
    seconds = float(real.group(1)) if real else None
    ran = criterion(
        "n1:zl80-full-frame-run",
        "pass" if seconds is not None and "exit 0" in log else "fail",
        f"4000 lines, wall {seconds} s, exit 0; evaluator lines {m['evaluated_lines']}",
    )
    return [ran, noise]


def w07() -> dict:
    d = json.loads(W07.read_text(encoding="utf-8"))["state_step_59999"]
    a, b = d["demo"], d["demo_offfilm"]
    differs = b["pmt.power_w"] > a["pmt.power_w"] and b["afe.v_adc"] != a["afe.v_adc"]
    return criterion(
        "browser:W07-paired-state",
        "partial" if differs else "fail",
        "off-film run changes front-end state at the same cursor; paired image/waveform "
        "visual comparison not executed",
    )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--no-tests", action="store_true", help="skip ruff and pytest")
    args = p.parse_args()
    py = [sys.executable, "-m"]
    criteria, steps = [], {}
    if not args.no_tests:
        steps["ruff"] = run(py + ["ruff", "check", "."])
        steps["pytest"] = run(py + ["pytest", "-q", "-p", "no:cacheprovider", "-m", "not slow"])
        for name, result in steps.items():
            tail = " | ".join(result["tail"])[-200:]
            criteria.append(
                criterion(f"tool:{name}", "pass" if result["returncode"] == 0 else "fail", tail)
            )
    else:
        criteria.append(criterion("tool:ruff+pytest", "not_executed", "--no-tests"))
    gate = archived.audit(args.out.with_name(args.out.stem + "_archived.json"))
    for c in gate["criteria"]:
        criteria.append(criterion("archived:" + c["name"], "pass" if c["passed"] else "fail",
                                  c["detail"]))  # fmt: skip
    criteria.append(low_light())
    criteria.extend(zl80())
    criteria.append(w07())
    criteria.extend(criterion(n, "not_executed", why) for n, why in NOT_EXECUTED)
    counts = {
        s: sum(c["status"] == s for c in criteria)
        for s in ("pass", "partial", "fail", "not_executed")
    }
    report = {
        "format": "zlscan-stage34-gates/1",
        "scope": "software gates for stages 3/4 on this host; no browser/hardware/measured claim",
        "counts": counts,
        "status": "fail" if counts["fail"] else "software_pass_with_open_items",
        "criteria": criteria,
        "steps": steps,
    }
    args.out.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    lines = [f"{c['status']:>12}  {c['name']}  {c['detail'][:90]}" for c in criteria]
    print("\n".join(lines))
    print(f"\n{report['status']}: {counts}")
    sys.exit(1 if counts["fail"] else 0)


if __name__ == "__main__":
    main()
