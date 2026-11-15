"""Evaluator for the ZL80 dual-range wedge run (ORACLE: uses the scene and the model gains).

Reads the delivered code image of a ``physical_stream`` run of a step-wedge scenario and reports,
per density step and per channel, the pixel-level density bias, standard deviation and invalid
fraction next to the analytic shot-noise budget. Samples are averaged in the linear domain per
pixel before the logarithm. This is an evaluation-side tool: it knows the true step positions,
the transimpedance ratio and the zero dark level, so its numbers are not a calibration result.

    .venv/bin/python tools/zl80_full_frame_metrics.py --run RUN_DIR --out metrics.json
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

from zlscan.budget.photons import sigma_density
from zlscan.engine.research_config import load_scenario

EDGE_GUARD_M = 0.002  # skip this much on both sides of every step edge
H, C = 6.62607015e-34, 299792458.0


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    loaded = load_scenario(args.run / "inputs" / "scenario.json")
    cfgs = [loaded.primary, loaded.secondary]
    sc = loaded.primary.scan
    (scene,) = loaded.primary.scenes.values()
    tmap = scene.transmittance_map
    if not np.all(tmap == tmap[0]):
        raise ValueError("evaluator expects a wedge that is uniform along the feed direction")
    image = json.loads((args.run / "observable" / "code_image.json").read_text())
    lines, n, channels = image["shape"]
    codes = np.memmap(args.run / "observable" / image["codes"], dtype="<u2", mode="r").reshape(
        lines, n, channels
    )
    # The first and last line sit on the film edge: part of the spot is off the film.
    codes = codes[1:-1]
    x = sc.x_center_m - sc.width_m / 2 + sc.width_m * (np.arange(n) + 0.5) / n
    pixel = np.minimum((np.arange(n) * sc.nx) // n, sc.nx - 1)
    cell_w = scene.width_m / tmap.shape[1]
    starts = [0, *(np.flatnonzero(np.diff(tmap[0])) + 1), tmap.shape[1]]
    bands = [
        (a * cell_w, b * cell_w, float(tmap[0, a]))
        for a, b in zip(starts, starts[1:], strict=False)
    ]
    pixel_time = sc.pitch_x_m / sc.v_x
    de, so, co = loaded.primary.detector, loaded.primary.source, loaded.primary.collect
    rate = so.power_w * co.eta_pre * co.eta_collect * de.wavelength_nm * 1e-9 / (H * C) * de.qe
    white_sel = (x < -EDGE_GUARD_M) | (x > scene.width_m + EDGE_GUARD_M)
    out = dict(
        schema="zlscan.zl80-full-frame-metrics/1",
        visibility="ORACLE_ONLY",
        evidence="assumed",
        lines=lines,
        evaluated_lines=lines - 2,
        samples_per_line=n,
        pixel_time_s=pixel_time,
        note="pixel = linear mean of ADC samples; white from off-film samples of channel 0; "
        "channel 1 scaled by the model transimpedance ratio; dark level is the model zero",
        channels=[],
    )
    ref = None
    for ch, cfg in enumerate(cfgs):
        ad, af = cfg.adc, cfg.afe
        lsb = (ad.vmax - ad.vmin) / 2**ad.bits
        top = 2**ad.bits - 1
        rows = []
        if ch == 0:
            white = codes[:, white_sel, 0].astype(np.float64)
            ref = float((ad.vmin + (white.mean() + 0.5) * lsb - af.v0) / af.r_t_ohm)
            white_saturated = float((white >= top).mean())
        for left, right, t_true in bands:
            lo, hi = left + EDGE_GUARD_M, right - EDGE_GUARD_M
            sel = (x >= lo) & (x < hi)
            px = pixel[sel]
            full = np.unique(px)[1:-1]  # drop partially covered pixels at both ends
            keep = sel & np.isin(pixel, full)
            block = np.asarray(codes[:, keep, ch])
            saturated = block >= top
            volts = ad.vmin + (block.astype(np.float64) + 0.5) * lsb
            current = (volts - af.v0) / af.r_t_ohm
            index = np.searchsorted(full, pixel[keep])
            counts = np.bincount(index, minlength=len(full))
            sums = np.stack([np.bincount(index, weights=r, minlength=len(full)) for r in current])
            bad = np.stack(
                [np.bincount(index, weights=r, minlength=len(full)) for r in saturated]
            ) > 0  # fmt: skip
            t_est = sums / counts / ref
            valid = (t_est > 0) & ~bad
            d_true = float(-math.log10(t_true))
            d = -np.log10(t_est[valid])
            n_e = rate * pixel_time * t_true
            rows.append(
                dict(
                    density_true=d_true,
                    pixels=int(valid.size),
                    invalid_fraction=float(1 - valid.mean()),
                    saturated_pixel_fraction=float(bad.mean()),
                    nonpositive_pixel_fraction=float((t_est <= 0).mean()),
                    bias_od=float(d.mean() - d_true) if d.size else None,
                    std_od=float(d.std(ddof=1)) if d.size > 1 else None,
                    budget_electrons_per_pixel=n_e,
                    budget_shot_sigma_od=sigma_density(n_e, de.excess_noise_factor),
                    budget_high_count_valid=bool(n_e >= 100),
                    mean_signal_codes=float(np.mean(volts) / lsb),
                )
            )
        entry = dict(channel=ch, transimpedance_ohm=af.r_t_ohm, steps=rows)
        if ch == 0:
            entry["white_reference_current_a"] = ref
            entry["white_saturated_fraction"] = white_saturated
        out["channels"].append(entry)
    args.out.write_text(json.dumps(out, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    for e in out["channels"]:
        for r in e["steps"]:
            print(
                f"ch{e['channel']} D={r['density_true']:.1f} bias={r['bias_od']} "
                f"std={r['std_od']} budget={r['budget_shot_sigma_od']:.4f} "
                f"invalid={r['invalid_fraction']:.4f} sat={r['saturated_pixel_fraction']:.3f}"
            )


if __name__ == "__main__":
    main()
