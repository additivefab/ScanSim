"""Replay a saved physical run from its on-disk analogue checkpoints."""

import hashlib
import json
from pathlib import Path

import numpy as np

from zlscan.engine.chain import ChainConfig
from zlscan.engine.config_bundle import configuration_for_run
from zlscan.replay.replay import CheckpointStore, replay_window


def replay_saved_window(
    source: Path, output: Path, cfg: ChainConfig | None, start_step: int, steps: int
) -> None:
    """Publish the requested ADC window to a new directory."""
    if output.resolve().is_relative_to(source.resolve()):
        raise ValueError("replay output must be separate from the source run")
    run = json.loads((source / "run.json").read_text(encoding="utf-8"))
    if run.get("mode") != "physical":
        raise ValueError("source is not a physical run")
    with configuration_for_run(source, run, cfg) as restored:
        _publish(source, output, restored, run, start_step, steps)


def _publish(
    source: Path, output: Path, cfg: ChainConfig, run: dict, start_step: int, steps: int
) -> None:
    fine_steps = run.get("fine_steps", 0)
    if start_step < 0 or steps < 1 or start_step + steps > fine_steps:
        raise ValueError("replay window falls outside the saved run")

    directory = source / "truth" / "checkpoints"
    candidates = [
        int(path.stem)
        for path in directory.glob("*.json")
        if path.stem.isdecimal() and int(path.stem) <= start_step
    ]
    if not candidates:
        raise ValueError("no checkpoint at or before the requested window")
    base = max(candidates)
    path = directory / f"{base}.json"
    if run.get("workbench") and hashlib.sha256(path.read_bytes()).hexdigest() != run["workbench"][
        "checkpoints"
    ].get(str(base)):
        raise ValueError("checkpoint hash mismatch")
    blob = path.read_bytes()
    if json.loads(blob).get("step") != base:
        raise ValueError("checkpoint step does not match its index")
    result = replay_window(cfg, CheckpointStore(0, {base: blob}), start_step, steps)

    output.mkdir(parents=True)
    np.savez(
        output / "samples.npz",
        codes=result.out.codes,
        saturated=result.out.saturated,
    )
    metadata = {
        "mode": "replay",
        "status": result.status.value,
        "source": str(source.resolve()),
        "config_hash": run["config_hash"],
        "start_step": start_step,
        "steps": steps,
        "checkpoint_step": base,
        "first_sample_index": result.out.first_sample,
        "discarded_steps": result.discarded_steps,
        "wall_s": result.wall_s,
    }
    (output / "run.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
