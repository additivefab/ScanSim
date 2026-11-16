"""Export the simulated feed-encoder counts of a physical run as ``zlscan-feed-encoder/1``.

Simulation-side tool (it reads the scenario, i.e. the truth): the output is labelled SIMULATED
and exists so a reconstruction with ``--encoder`` can be exercised. Needs ``scan.encoder_pitch_m``.

    .venv/bin/python tools/export_feed_encoder.py --run RUN_DIR --out encoder.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from zlscan.engine.research_config import load_scenario
from zlscan.engine.rng import CounterRng
from zlscan.io.manifest import sha256_file
from zlscan.truth import scan as scan_mod


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    loaded = load_scenario(args.run / "inputs" / "scenario.json")
    cfg = loaded.primary
    if cfg.scan.encoder_pitch_m is None:
        raise SystemExit("scan.encoder_pitch_m is not set: the simulated instrument has no encoder")
    lines = loaded.document.lines
    rng = CounterRng(cfg.effective_common_seed)
    starts = scan_mod.line_start_s(cfg.scan, np.arange(lines), rng, cfg.domain_id)
    counts = scan_mod.encoder_counts(cfg.scan, starts)
    doc = {
        "format": "zlscan-feed-encoder/1",
        "raw_sha256": sha256_file(args.run / "observable" / "raw.zlraw"),
        "pitch_m": cfg.scan.encoder_pitch_m,
        "counts": [int(c) for c in counts],
        "origin": "SIMULATED",
    }
    args.out.write_text(json.dumps(doc) + "\n", encoding="utf-8")
    print(f"{lines} counts, first {doc['counts'][0]}, last {doc['counts'][-1]}")


if __name__ == "__main__":
    main()
