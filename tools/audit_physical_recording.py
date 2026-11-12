"""Resume one analog/control/digital/file root in fresh processes, including abrupt exits."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from zlscan.adc_link.link import LinkConfig
from zlscan.config.presets import legacy_dc_config
from zlscan.digital.acquisition import AcquisitionConfig
from zlscan.engine.physical_recording import PhysicalRecorder
from zlscan.io.manifest import sha256_file
from zlscan.io.rawstream import read_all
from zlscan.transport.queues import StageSpec


def setup(edge, stalled):
    a = legacy_dc_config(1.0)
    a.detector = replace(a.detector, pole_hz=70_000, shot_noise=True)
    a.afe = replace(a.afe, pole_hz=50_000, aa_poles_hz=(90_000,))
    b = deepcopy(a)
    b.seed = 31
    link = LinkConfig(
        latency_half_cycles=25,
        propagation_ps=1500,
        capture_edge=edge,
        channel_order=(1, 0),
        coding="twos_complement",
    )
    stages = [
        StageSpec("rx", 232, 2e9),
        StageSpec(
            "disk",
            116,
            1e8 if stalled else 1e12,
            stalls=((40_000_000, 500_000_000),) if stalled else (),
        ),
    ]
    return {0: a, 1: b}, link, AcquisitionConfig(2, 13), stages


def create(directory, edge, stalled):
    recorder = PhysicalRecorder(
        directory, *setup(edge, stalled), session_id=19, run_id="physical", branch_id="main"
    )
    recorder.schedule_syncs([recorder.domain.tick_of(i) for i in (20, 50, 80, 110)])
    return recorder


def prefix(recorder):
    recorder.advance(57)
    recorder.request(
        0, "detector.gain", 2400, effective_step=113, readback_step=351, readback_value=2400
    )
    recorder.request(1, "source.power_w", 4e-3, effective_step=381)
    recorder.advance(284)


def continuation(recorder):
    recorder.advance(103)
    recorder.reset_control()
    recorder.start()
    recorder.advance(347)
    recorder.finish()


def child(directory, edge, stalled, branch, crash):
    start = time.perf_counter_ns()
    with PhysicalRecorder.restore(
        directory,
        *setup(edge, stalled),
        (directory / "checkpoint.json").read_bytes(),
        branch_directory=branch,
    ) as recorder:
        restored_ms = (time.perf_counter_ns() - start) / 1e6
        assert recorder.step == 341 and recorder.applied_commands == 1
        if crash:
            recorder.advance(103)
            os._exit(86)  # No close/cleanup and no newer root is published.
        continuation(recorder)
        recorder.save_checkpoint()
        elapsed_ms = (time.perf_counter_ns() - start) / 1e6
        (recorder.directory / "continuation.json").write_text(
            json.dumps(
                {
                    "read_restore_ms": restored_ms,
                    "read_restore_continue_commit_ms": elapsed_ms,
                    "summary": recorder.summary(),
                }
            )
        )


def audit(directory):
    directory.mkdir(parents=True, exist_ok=False)
    cases = []
    names = ("raw.zlraw", "events.jsonl", "checkpoint.json")
    for edge in ("rising", "falling"):
        for stalled in (False, True):
            for crash in (False, True):
                label = f"{edge}_{int(stalled)}_{int(crash)}"
                baseline = directory / f"baseline_{label}"
                with create(baseline, edge, stalled) as recorder:
                    prefix(recorder)
                    continuation(recorder)
                    recorder.save_checkpoint()
                    expected = recorder.summary()
                actual = directory / f"actual_{label}"
                with create(actual, edge, stalled) as recorder:
                    prefix(recorder)
                    assert recorder.applied_commands == 1 and len(recorder.commands) == 2
                    pending = recorder.recording.pipeline.link.pending_count
                    queued = recorder.recording.pipeline.pending_packet_count
                    active = recorder.recording.pipeline.acquisition.active
                    retained = sum(len(s) for s in active.segments) if active else 0
                    committed = recorder.recording.writer.next_sequence
                    assert pending > 0 and retained > 0 and committed > 0
                    if stalled:
                        assert queued > 0
                    start = time.perf_counter_ns()
                    checkpoint = recorder.save_checkpoint()
                    publish_ms = (time.perf_counter_ns() - start) / 1e6
                    checkpoint_bytes = checkpoint.stat().st_size
                prefixes = {name: (actual / name).read_bytes() for name in names[:2]}
                root_hash = sha256_file(checkpoint)
                inode = (actual / "raw.zlraw").stat().st_ino
                command = [sys.executable, __file__, "--resume", str(actual), "--edge", edge]
                if stalled:
                    command.append("--stalled")
                crash_returncode = None
                if crash:
                    killed = subprocess.run(
                        [*command, "--crash-tail"], capture_output=True, text=True, timeout=30
                    )
                    assert killed.returncode == 86, killed.stdout + killed.stderr
                    crash_returncode = killed.returncode
                    original = {name: sha256_file(actual / name) for name in names}
                    assert original["checkpoint.json"] == root_hash
                    try:
                        PhysicalRecorder.restore(
                            actual, *setup(edge, stalled), checkpoint.read_bytes()
                        )
                    except ValueError as exc:
                        assert "tail" in str(exc)
                    else:
                        raise AssertionError("uncommitted tail accepted")
                    destination = directory / f"branch_{label}"
                    command.extend(["--branch", str(destination)])
                else:
                    destination = actual
                result = subprocess.run(command, capture_output=True, text=True, timeout=30)
                assert result.returncode == 0, result.stdout + result.stderr
                continued = json.loads((destination / "continuation.json").read_text())
                assert continued["summary"] == expected
                if crash:
                    assert original == {name: sha256_file(actual / name) for name in names}
                else:
                    assert (actual / "raw.zlraw").stat().st_ino == inode
                for name in names:
                    assert (destination / name).read_bytes() == (baseline / name).read_bytes()
                for name, data in prefixes.items():
                    assert (destination / name).read_bytes().startswith(data)
                raw = read_all(destination / "raw.zlraw")
                assert not raw.errors and not raw.fatal
                cases.append(
                    {
                        "edge": edge,
                        "stalled": stalled,
                        "abrupt_exit": crash,
                        "committed_raw_blocks_at_checkpoint": committed,
                        "adc_pending_samples": pending,
                        "unfinished_window_samples": retained,
                        "queued_payloads": queued,
                        "future_physical_commands": 1,
                        "checkpoint_bytes": checkpoint_bytes,
                        "publish_ms": publish_ms,
                        "raw_journal_root_and_summary_match": True,
                        "same_inode_in_place": not crash,
                        "original_fault_files_preserved": crash,
                        "crash_returncode": crash_returncode,
                        "child_returncode": result.returncode,
                        "child_command": command,
                        "final_sha256": {name: sha256_file(destination / name) for name in names},
                        **continued,
                    }
                )
    report = {
        "passed": True,
        "seed": [1, 31],
        "cases": cases,
        "scope": "Assumed noisy dual-channel model with explicit pre-log response. "
        "Actual analog/control/digital/file-root restore and append. Logical control reset "
        "preserves physical parameters/history and digital in-flight data. "
        "Excludes hardware reset, power loss, measured readbacks, trace-file cursors and CLI/UI.",
        "timing_scope": "Root fsync/atomic publication; new-process read/restore; continuation "
        "adds 450 fine steps per channel, logical reset/start, EOF/drain and fsync/publication. "
        "Excludes process startup. Disk cache uncontrolled.",
    }
    (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--edge", choices=("rising", "falling"))
    parser.add_argument("--stalled", action="store_true")
    parser.add_argument("--branch", type=Path)
    parser.add_argument("--crash-tail", action="store_true")
    args = parser.parse_args()
    if args.resume:
        child(args.resume, args.edge, args.stalled, args.branch, args.crash_tail)
    else:
        report = audit(args.out)
        print(
            f"{len(report['cases'])} physical-root recoveries matched files and complete snapshots"
        )
