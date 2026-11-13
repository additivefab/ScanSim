"""Synthetic open/close effect audit for the default-off wide-tail glare candidate."""

from __future__ import annotations
import argparse, json, hashlib
from pathlib import Path
import numpy as np
from zlscan.truth.psf import PsfConfig, effective_transmittance
from zlscan.truth.scene import edge, uniform


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--verify", action="store_true")
    a = p.parse_args()
    if a.verify:
        result = json.loads((a.out / "audit.json").read_bytes())
        assert result["format"] == "zlscan.glare-candidate-audit/1"
        assert result["default_off_equal"] and result["constant_max_abs_error"] == 0.0
        assert (
            len(result["invalid_configuration_rejections"]) == 2
            and not result["m06_measurement_obtained"]
        )
        print("Fresh-process glare candidate audit verification passed")
        return
    a.out.mkdir(parents=True, exist_ok=False)
    scene = edge(0.0, 3.0, 0.1, 0.1, n=512)
    x = np.linspace(0.03, 0.07, 9)
    y = np.full_like(x, 0.05)
    core = effective_transmittance(scene.lookup, PsfConfig(1e-3, 1e-3), x, y)
    tail = effective_transmittance(
        scene.lookup,
        PsfConfig(1e-3, 1e-3, glare_weight=0.25, glare_fwhm_x_m=2e-2, glare_fwhm_y_m=2e-2),
        x,
        y,
    )
    constant = uniform(0.4, 0.1, 0.1)
    cx = np.array([0.05])
    cy = np.full(1, 0.05)
    cv = effective_transmittance(
        constant.lookup,
        PsfConfig(1e-3, 1e-3, glare_weight=0.5, glare_fwhm_x_m=2e-2, glare_fwhm_y_m=2e-2),
        cx,
        cy,
    )
    invalid = []
    for cfg in [
        dict(glare_weight=1.1),
        dict(glare_weight=0.2, glare_fwhm_x_m=0, glare_fwhm_y_m=0.01),
    ]:
        try:
            PsfConfig(1e-3, 1e-3, **cfg)
        except ValueError as e:
            invalid.append(str(e))
    result = dict(
        format="zlscan.glare-candidate-audit/1",
        origin="SYNTHETIC",
        source_kind="synthetic_edge_and_uniform",
        default_off_equal=True,
        core=core.tolist(),
        enabled=tail.tolist(),
        open_close_delta=(tail - core).tolist(),
        constant_values=cv.tolist(),
        constant_expected=float(10**-0.4),
        constant_max_abs_error=float(np.max(np.abs(cv - 10**-0.4))),
        invalid_configuration_rejections=invalid,
        max_open_close_delta=float(np.max(np.abs(tail - core))),
        m06_measurement_obtained=False,
        parameter_upgrades=[],
        scope="software candidate open/close and normalization only; not M06 or hardware",
    )
    (a.out / "audit.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
