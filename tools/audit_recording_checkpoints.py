"""Resume actual raw/journal files in fresh processes, including an abrupt uncommitted-tail exit."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from audit_digital_checkpoints import ACQUISITION, DOMAIN, SEED, block, setup
from zlscan.engine.digital_pipeline import DigitalPipeline
from zlscan.engine.digital_recording import DigitalRecorder
from zlscan.io.manifest import sha256_file
from zlscan.io.rawstream import read_all


def create(directory, edge, stalled):
    link, stages, clock = setup(edge, stalled)
    engine = DigitalPipeline(DOMAIN, link, ACQUISITION, stages, (0, 1))
    engine.schedule_syncs([clock.tick_of(2**32 - 5 + offset) for offset in (12, 30, 48, 66)])
    return DigitalRecorder(directory, engine, 19)


def child(root, spec_path, branch, crash):
    spec = json.loads(spec_path.read_text())
    link, stages, _ = setup(spec["edge"], spec["stalled"])
    codes = np.load(spec_path.parent / "codes.npy", allow_pickle=False)
    blob = (root / "checkpoint.json").read_bytes()
    start = time.perf_counter_ns()
    with DigitalRecorder.restore(
        root, DOMAIN, link, ACQUISITION, stages, (0, 1), blob, branch_directory=branch
    ) as recorder:
        restored_ms = (time.perf_counter_ns() - start) / 1e6
        assert recorder.next_source_index == 2**32 - 5 + 38
        recorder.push(block(codes, 38, 73))
        if crash:
            os._exit(
                86
            )  # Deliberately skip Python close/cleanup and any new checkpoint publication.
        recorder.finish()
        summary = recorder.summary()
        recorder.save_checkpoint()
        destination = branch or root
        (destination / "continuation.json").write_text(
            json.dumps(
                {
                    "restore_files_pipeline_ms": restored_ms,
                    "restore_continue_commit_ms": (time.perf_counter_ns() - start) / 1e6,
                    "summary": summary,
                }
            )
        )


def audit(directory):
    directory.mkdir(parents=True, exist_ok=False)
    codes = np.random.default_rng(SEED).integers(1, 65535, (73, 2), dtype=np.uint16)
    codes[[3, 24, 25, 57, 63], 0] = [0, 65535, 0, 65535, 0]
    np.save(directory / "codes.npy", codes)
    cases = []
    for edge in ("rising", "falling"):
        for stalled in (False, True):
            for crash in (False, True):
                label = f"{edge}_{int(stalled)}_{int(crash)}"
                baseline, actual = directory / f"baseline_{label}", directory / f"actual_{label}"
                with create(baseline, edge, stalled) as recorder:
                    for start, stop in ((0, 25), (25, 38), (38, 73)):
                        recorder.push(block(codes, start, stop))
                    recorder.finish()
                    expected = recorder.summary()
                with create(actual, edge, stalled) as recorder:
                    recorder.push(block(codes, 0, 25))
                    recorder.push(block(codes, 25, 38))
                    start = time.perf_counter_ns()
                    checkpoint = recorder.save_checkpoint()
                    save_ms = (time.perf_counter_ns() - start) / 1e6
                prefix = {
                    name: (actual / name).read_bytes() for name in ("raw.zlraw", "events.jsonl")
                }
                inode = (actual / "raw.zlraw").stat().st_ino
                checkpoint_hash = sha256_file(checkpoint)
                checkpoint_bytes = checkpoint.stat().st_size
                spec = directory / f"spec_{label}.json"
                spec.write_text(json.dumps({"edge": edge, "stalled": stalled}))
                command = [sys.executable, __file__, "--resume", str(actual), "--spec", str(spec)]
                crash_returncode = None
                if crash:
                    killed = subprocess.run(
                        [*command, "--crash-tail"], capture_output=True, text=True, timeout=30
                    )
                    assert killed.returncode == 86, killed.stdout + killed.stderr
                    crash_returncode = killed.returncode
                    assert sha256_file(checkpoint) == checkpoint_hash
                    original = {name: sha256_file(actual / name) for name in prefix}
                    link, stages, _ = setup(edge, stalled)
                    try:
                        DigitalRecorder.restore(
                            actual,
                            DOMAIN,
                            link,
                            ACQUISITION,
                            stages,
                            (0, 1),
                            checkpoint.read_bytes(),
                        )
                    except ValueError as exc:
                        assert "tail" in str(exc)
                    else:
                        raise AssertionError("uncommitted tail was not refused")
                    destination = directory / f"branch_{label}"
                    command.extend(["--branch", str(destination)])
                else:
                    destination = actual
                completed = subprocess.run(command, capture_output=True, text=True, timeout=30)
                assert completed.returncode == 0, completed.stdout + completed.stderr
                continuation = json.loads((destination / "continuation.json").read_text())
                assert continuation["summary"] == expected
                if crash:
                    assert original == {name: sha256_file(actual / name) for name in prefix}
                    assert sha256_file(checkpoint) == checkpoint_hash
                else:
                    assert (actual / "raw.zlraw").stat().st_ino == inode
                for name, contents in prefix.items():
                    actual_bytes = (destination / name).read_bytes()
                    assert actual_bytes[: len(contents)] == contents
                    assert actual_bytes == (baseline / name).read_bytes()
                raw = read_all(destination / "raw.zlraw")
                assert not raw.fatal and not raw.errors
                assert [block.seq for block in raw.blocks] == list(range(len(raw.blocks)))
                cases.append(
                    {
                        "edge": edge,
                        "stalled": stalled,
                        "abrupt_exit": crash,
                        "checkpoint_bytes": checkpoint_bytes,
                        "checkpoint_publish_ms": save_ms,
                        **continuation,
                        "prefix_raw_bytes": len(prefix["raw.zlraw"]),
                        "final_raw_bytes": (destination / "raw.zlraw").stat().st_size,
                        "raw_sha256": sha256_file(destination / "raw.zlraw"),
                        "journal_sha256": sha256_file(destination / "events.jsonl"),
                        "raw_journal_and_summary_match": True,
                        "original_fault_files_preserved": crash,
                        "same_raw_inode_in_place": not crash,
                        "crash_returncode": crash_returncode,
                        "child_command": command,
                        "child_returncode": completed.returncode,
                    }
                )
    report = {
        "seed": SEED,
        "passed": True,
        "cases": cases,
        "scope": (
            "Actual committed-file append and preserved-tail branch recovery. "
            "Synthetic digital model; excludes analog, controller, CLI/UI and physical power loss."
        ),
        "timing_scope": (
            "Checkpoint flush/fsync+atomic publication; file/pipeline restore; "
            "continuation adds 35 inputs, EOF/drain, writes/fsyncs outputs and publishes. "
            "Excludes process startup. Disk cache uncontrolled."
        ),
    }
    (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--branch", type=Path)
    parser.add_argument("--crash-tail", action="store_true")
    args = parser.parse_args()
    if args.resume:
        child(args.resume, args.spec, args.branch, args.crash_tail)
    else:
        report = audit(args.out)
        print(f"{len(report['cases'])} recording recoveries matched raw and journal bytes")
