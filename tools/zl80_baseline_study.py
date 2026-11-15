"""ZL80 baseline study: analytic budget over power x gain x polygon speed (all inputs ASSUMED).

Each grid point is the public scenario budget of ``configs/zl80_baseline.json`` with three chain
fields replaced. Nothing here is a simulation run or a hardware fact.

    .venv/bin/python tools/zl80_baseline_study.py --out docs/reports/zl80_baseline_data.json
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
from dataclasses import replace
from pathlib import Path

from zlscan.budget.photons import electrons_needed
from zlscan.budget.runner import run_budget
from zlscan.engine.research_config import load_scenario, scenario_budget

POWERS_W = (5e-5, 2e-4, 1e-3)
GAINS = (100.0, 250.0, 500.0, 1000.0)
RPMS = (750.0, 1500.0, 3000.0, 6000.0)
TARGET_SIGMAS_OD = (0.02, 0.05)  # illustrative only; the product tolerance is undecided (Q016)


def point(loaded, power_w: float, gain: float, rpm: float) -> dict:
    cfg = loaded.primary
    cfg = replace(
        cfg,
        source=replace(cfg.source, power_w=power_w),
        detector=replace(cfg.detector, gain=gain),
        scan=replace(cfg.scan, rpm=rpm),
    )
    rows = {c["id"]: c for c in run_budget(scenario_budget(replace(loaded, primary=cfg)))[
        "conclusions"
    ]}  # fmt: skip
    top = max(float(k.rsplit("D", 1)[1]) for k in rows if k.startswith("photons.sigma.D"))
    scene_height = next(iter(cfg.scenes.values())).height_m
    return dict(
        power_w=power_w,
        gain=gain,
        rpm=rpm,
        line_rate_hz=rows["geometry.f_line_hz"]["value"],
        pixel_rate_hz=rows["geometry.pixel_rate_hz"]["value"],
        film_scan_time_s=scene_height / cfg.scan.v_y,
        top_density=top,
        electrons_top=rows[f"photons.electrons.D{top:g}"]["value"],
        sigma_od={f"{d:g}": rows[f"photons.sigma.D{d:g}"]["value"] for d in (3.0, 4.0, top)},
        anode_d0_a=rows["photons.anode.D0"]["value"],
        anode_d0_status=rows["photons.anode.D0"]["status"],
        adc_d0_v=rows["signals.adc_v.D0"]["value"],
        adc_d0_status=rows["signals.adc_v.D0"]["status"],
        codes_top=rows[f"signals.codes.D{top:g}"]["value"],
        feasible_d0=rows["photons.anode.D0"]["status"] == "PASS"
        and rows["signals.adc_v.D0"]["status"] == "PASS",
    )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, default=Path("configs/zl80_baseline.json"))
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    loaded = load_scenario(args.config)
    points = [point(loaded, *g) for g in itertools.product(POWERS_W, GAINS, RPMS)]
    cfg = loaded.primary
    base = point(loaded, cfg.source.power_w, cfg.detector.gain, cfg.scan.rpm)
    needed = {}
    for target in TARGET_SIGMAS_OD:
        n = electrons_needed(target, cfg.detector.excess_noise_factor)
        factor = n / base["electrons_top"]  # power x pixel-time product relative to baseline
        needed[f"{target:g}"] = dict(
            electrons_per_pixel=n,
            exposure_factor_vs_baseline=factor,
            power_w_at_baseline_speed=cfg.source.power_w * factor,
            rpm_at_baseline_power=cfg.scan.rpm / factor,
            film_scan_time_s_at_baseline_power=base["film_scan_time_s"] * factor,
            high_count_formula_valid=n >= 100,
        )
    out = dict(
        schema="zlscan.zl80-baseline-study/1",
        evidence="assumed",
        note="Analytic DC/shot-noise budget only; no PSF, dynamics, glare, electronics noise.",
        scenario=loaded.provenance(),
        baseline=base,
        exposure_needed_at_top_density=needed,
        grid=points,
    )
    assert all(math.isfinite(v["codes_top"]) for v in points)
    args.out.write_text(json.dumps(out, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"{len(points)} grid points; {sum(v['feasible_d0'] for v in points)} pass D=0 limits")


if __name__ == "__main__":
    main()
