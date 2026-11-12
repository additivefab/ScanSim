"""Replay a saved physical run from its on-disk analogue checkpoints."""

import json
from pathlib import Path

import numpy as np

from zlscan.engine.chain import ChainConfig
from zlscan.replay.replay import CheckpointStore, replay_window


def replay_saved_window(
    source: Path, output: Path, cfg: ChainConfig, start_step: int, steps: int
) -> None:
    """Publish the requested ADC window to a new directory."""
    if output.resolve().is_relative_to(source.resolve()):
        raise ValueError("replay output must be separate from the source run")
    run = json.loads((source / "run.json").read_text(encoding="utf-8"))
    if run.get("mode") != "physical" or run.get("config_hash") != cfg.hash():
        raise ValueError("source configuration does not match the physical preset")
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
    blob = (directory / f"{base}.json").read_bytes()
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
