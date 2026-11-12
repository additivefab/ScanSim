"""Run wedge and uniform-patch quality metrics on the fixed nominal CLI experiment."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from zlscan.cli import main


def run(source: Path, output: Path) -> int:
    truth_path = source / "evaluation_inputs/unblurred.npy"
    truth = np.load(truth_path, allow_pickle=False)
    levels = np.array([0.2, 0.8, 1.4, 2.0])  # Assumed nominal step-wedge target.
    if truth.shape != (12, 40):
        raise ValueError("this experiment requires the fixed 12x40 nominal frame")
    distances = np.abs(truth[..., None] - levels)
    if not np.all(np.min(distances, axis=-1) < 1e-12):
        raise ValueError("reference does not match the declared nominal wedge")
    geometry = json.loads((source / "reconstructed/run.json").read_text())["grid"]
    output.mkdir(parents=True, exist_ok=False)
    inputs = output / "inputs"
    inputs.mkdir()
    np.save(inputs / "labels.npy", np.argmin(distances, axis=-1))
    requests = [
        {"name": "wedge", "kind": "wedge", "labels": "labels.npy", "levels_od": levels.tolist()}
    ]
    for index, (first, last) in enumerate(((3, 7), (13, 17), (23, 27), (33, 37))):
        requests.append(
            {
                "name": f"noise_step_{index}",
                "kind": "noise",
                "patches": [[2, 6, first, last], [6, 10, first, last]],
                "pitch_x_m": geometry["pitch_x_m"],
                "pitch_y_m": geometry["pitch_y_m"],
            }
        )
    targets = inputs / "targets.json"
    targets.write_text(
        json.dumps({"format": "zlscan-quality-targets/1", "evaluations": requests}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    return main(
        [
            "metrics",
            "--image",
            str(source / "reconstructed/density.npz"),
            "--truth",
            str(truth_path),
            "--optical",
            str(source / "evaluation_inputs/optical.npy"),
            "--roi",
            str(source / "evaluation_inputs/roi.npy"),
            "--targets",
            str(targets),
            "--out",
            str(output / "metrics"),
        ]
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    raise SystemExit(run(args.source, args.out))
