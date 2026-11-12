"""Persist and restore busy transport queues in fresh Python processes; functional evidence only."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

from zlscan.transport.queues import Item, StageSpec, TransportSimulation, simulate


def child(spec_path: Path, checkpoint_path: Path, output: Path) -> None:
    stages = [StageSpec(**row) for row in json.loads(spec_path.read_text())]
    start = time.perf_counter_ns()
    engine = TransportSimulation.restore(stages, checkpoint_path.read_bytes())
    engine.advance()
    elapsed_ms = (time.perf_counter_ns() - start) / 1e6
    output.write_text(
        json.dumps({"result": asdict(engine.result()), "restore_replay_ms": elapsed_ms})
    )


def audit(directory: Path) -> dict:
    directory.mkdir(parents=True, exist_ok=False)
    scale, origin = 1_000_000, 2**53 + 17
    items = [
        Item(i, 4, i // 4, 1, origin + i * 4, 4, origin + tick * scale, i)
        for i, tick in enumerate([0, 0, 4, 8, 12, 12, 20, 30, 100])
    ]
    reports = []
    for mode in (1, 2, 3):
        stages = [
            StageSpec("rx", 24, 4e6),
            StageSpec(
                "disk",
                8,
                1e6,
                stalls=((origin + 5 * scale, 50 * scale),),
                backpressure=mode != 2,
                fail_windows=((origin + 60 * scale, 20 * scale),) if mode == 3 else (),
            ),
        ]
        expected = json.loads(json.dumps(asdict(simulate(items, stages))))
        spec_path = directory / f"stages_{mode}.json"
        spec_path.write_text(json.dumps([asdict(stage) for stage in stages]))
        for cut in (15, 55):
            engine = TransportSimulation(stages, items)
            engine.advance(origin + cut * scale)
            checkpoint_path = directory / f"state_{mode}_{cut}.json"
            start = time.perf_counter_ns()
            checkpoint_path.write_bytes(engine.checkpoint())
            save_ms = (time.perf_counter_ns() - start) / 1e6
            checkpoint = checkpoint_path.read_bytes()
            assert engine.checkpoint() == checkpoint
            output = directory / f"resumed_{mode}_{cut}.json"
            command = [
                sys.executable,
                __file__,
                "--resume",
                str(checkpoint_path),
                "--stages",
                str(spec_path),
                "--out",
                str(output),
            ]
            completed = subprocess.run(command, capture_output=True, text=True, timeout=30)
            assert completed.returncode == 0, completed.stdout + completed.stderr
            resumed = json.loads(output.read_text())
            assert resumed["result"] == expected
            state = json.loads(checkpoint)["state"]
            reports.append(
                {
                    "mode": mode,
                    "cut_tick_ps": str(origin + cut * scale),
                    "checkpoint_bytes": len(checkpoint),
                    "serialize_write_ms": save_ms,
                    "restore_replay_ms": resumed["restore_replay_ms"],
                    "pending_events": len(state["events"]),
                    "blocked_stages": sum(row["blocked"] is not None for row in state["stages"]),
                    "serving_stages": sum(row["serving"] is not None for row in state["stages"]),
                    "queued_items": sum(len(row["queue"]) for row in state["stages"]),
                    "child_command": command,
                    "child_returncode": completed.returncode,
                    "complete_result_matches_continuous": True,
                    "delivered_items": len(resumed["result"]["delivered"]),
                    "lost_items": len(resumed["result"]["losses"]),
                }
            )
    report = {
        "scope": (
            "Transport queues only; synthetic assumed service; "
            "no ADC/acquisition/control/disk cursor"
        ),
        "timing_scope": (
            "Serialization+file write, and file read+restore+replay; "
            "excludes child startup; disk cache uncontrolled; "
            "no fsync durability claim"
        ),
        "cases": reports,
        "passed": True,
    }
    (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--stages", type=Path)
    args = parser.parse_args()
    if args.resume:
        child(args.stages, args.resume, args.out)
    else:
        report = audit(args.out)
        print(f"{len(report['cases'])} persisted transport checkpoints matched in fresh processes")
