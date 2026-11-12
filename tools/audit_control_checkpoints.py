"""Restore control ledgers with future declarations in a fresh process for every state."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from zlscan.control.state import Controller, State
from zlscan.io.manifest import sha256_file

BASE = 2**53 + 17


def setup(state: State) -> Controller:
    control = Controller()
    route = [
        State.CONFIGURED,
        State.SETTLING,
        State.CALIBRATING,
        State.READY,
        State.SCANNING,
        State.DRAINING,
        State.DONE,
    ]
    if state in (State.FAULT, State.ABORTED):
        control.to(state, 0)
    elif state != State.UNCONFIGURED:
        for target in route:
            control.to(target, 0)
            if target == state:
                break
    control.request("gain", {"range": [1, 2]}, BASE, BASE + 10, BASE + 20, {"range": [1, 2]})
    return control


def continue_control(control: Controller) -> None:
    assert not control.readback_confirmed("gain", at_tick=BASE + 19)
    assert control.readback_confirmed("gain", at_tick=BASE + 20)
    control.reset(BASE + 30)
    assert not control.readback_confirmed("gain")
    control.to(State.CONFIGURED, BASE + 30)
    control.request("gain", {"range": [1, 2]}, BASE + 31, BASE + 40)
    assert not control.readback_confirmed("gain")


def child(source: Path, destination: Path) -> None:
    start = time.perf_counter_ns()
    control = Controller.restore(source.read_bytes())
    assert control.checkpoint() == source.read_bytes()
    restore_ms = (time.perf_counter_ns() - start) / 1e6
    continue_control(control)
    data = control.checkpoint()
    elapsed_ms = (time.perf_counter_ns() - start) / 1e6
    destination.write_bytes(data)
    print(
        json.dumps(
            {
                "read_restore_verify_ms": restore_ms,
                "read_restore_verify_continue_snapshot_ms": elapsed_ms,
            }
        )
    )


def audit(directory: Path) -> dict:
    directory.mkdir(parents=True, exist_ok=False)
    cases = []
    for state in State:
        control = setup(state)
        source = directory / f"{state.value}_checkpoint.json"
        source.write_bytes(control.checkpoint())
        before = sha256_file(source)
        continue_control(control)
        expected = control.checkpoint()
        destination = directory / f"{state.value}_continued.json"
        command = [
            sys.executable,
            __file__,
            "--resume",
            str(source),
            "--continued",
            str(destination),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        assert destination.read_bytes() == expected
        assert sha256_file(source) == before
        cases.append(
            {
                "state": state.value,
                "checkpoint_bytes": source.stat().st_size,
                "source_sha256": before,
                "continued_sha256": sha256_file(destination),
                "source_unchanged": True,
                "continuation_bytes_match": True,
                "child_command": command,
                "child_returncode": result.returncode,
                **json.loads(result.stdout),
            }
        )
    report = {
        "passed": True,
        "cases": cases,
        "scope": "Functional control ledger only; all times/configuration values assumed. "
        "Future declarations are not executed hardware confirmations. "
        "No physical parameter changes or analog/digital/file composite checkpoint.",
        "timing_scope": "New process read/restore/replay-validation and control continuation. "
        "Excludes process startup and result write. Disk cache uncontrolled.",
    }
    (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--continued", type=Path)
    args = parser.parse_args()
    if args.resume:
        child(args.resume, args.continued)
    else:
        report = audit(args.out)
        print(f"{len(report['cases'])} control-state continuations matched checkpoint bytes")
