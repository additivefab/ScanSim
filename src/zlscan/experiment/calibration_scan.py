"""Generate simulated reference scans on a single uninterrupted acquisition timeline."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np

from zlscan.contracts.calibration import Binding
from zlscan.engine.chain import ChainConfig, Phase
from zlscan.engine.physical_run import run_physical
from zlscan.io.manifest import sha256_file
from zlscan.truth.scene import Scene, edge, step_wedge, uniform

_TARGETS = ("dark", "white", "wedge", "edge", "grid", "slanted_line")
_DENSITIES = (0.0, 0.5, 1.0, 1.5, 2.0)


def run_calibration_scan(
    out: Path, instrument: ChainConfig, *, lines_per_target: int = 8
) -> dict[str, Any]:
    """Write observable codes and the known reference-target acquisition protocol."""
    if lines_per_target < 2:
        raise ValueError("calibration requires at least two lines per target")
    scan = instrument.scan
    if scan.x_center_m != scan.width_m / 2 or scan.y0_m != 0:
        raise ValueError("reference targets require a scan origin at the film corner")

    lines = lines_per_target * len(_TARGETS)
    width_m = scan.width_m
    height_m = (lines + 1) * scan.pitch_y_m
    rows, columns = np.ogrid[:512, :512]
    x = (columns + 0.5) / 512
    y = (rows + 0.5) / 512 * (lines + 1)
    grid = (np.abs((x * 8) % 1 - 0.5) < 0.1) | (np.abs((y % 4) - 2) < 0.5)
    slant_position = 0.2 + 0.6 * (y - 5 * lines_per_target) / lines_per_target
    slanted_line = np.abs(x - slant_position) < 0.025
    scenes = {
        "white": uniform(0.0, width_m, height_m),
        "wedge": step_wedge(list(_DENSITIES), width_m, height_m),
        "edge": edge(0.0, 1.0, width_m, height_m),
        "grid": Scene.from_density(grid.astype(float), width_m, height_m),
        "slanted_line": Scene.from_density(slanted_line.astype(float), width_m, height_m),
    }
    timeline = [
        Phase(
            name,
            index * lines_per_target,
            lines_per_target,
            "white" if name == "dark" else name,
            laser_on=name != "dark",
        )
        for index, name in enumerate(_TARGETS)
    ]
    cfg = replace(instrument, scenes=scenes, timeline=timeline)
    run = run_physical(out, cfg, lines)
    observable = out / "observable"
    protocol = {
        "version": 1,
        "origin": "SIMULATED",
        "binding": Binding(
            simulation_hash=replace(
                instrument, seed=0, evidence={}, scenes={}, timeline=[], injections=[]
            ).hash(),
            channel_roles={"0": None},
        ).model_dump(mode="json"),
        "raw_sha256": sha256_file(observable / "raw.zlraw"),
        "width_m": width_m,
        "pitch_y_m": scan.pitch_y_m,
        "targets": [
            {"name": phase.name, "first_line": phase.first_line, "lines": phase.n_lines}
            for phase in timeline
        ],
        "wedge_density": list(_DENSITIES),
        "edge_fraction": 0.5,
        "grid_line_x_fraction": [(index + 0.5) / 8 for index in range(8)],
        "slanted_line_x_fractions": [0.2, 0.8],
    }
    (observable / "scan_protocol.json").write_text(
        json.dumps(protocol, indent=2) + "\n", encoding="utf-8"
    )
    return run
