"""Public JSONL recorder restart and actual process-kill prefix-branch audit."""

from __future__ import annotations

import argparse
import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from zlscan.io.manifest import sha256_file
from zlscan.io.rawstream import scan_blocks


def hashes(path):
    return {p.relative_to(path).as_posix(): sha256_file(p) for p in path.rglob("*") if p.is_file()}


def verify(root):
    expected = json.loads((root / "fault_manifest.json").read_text())
    assert hashes(root / "fault") == expected
    files = ["raw.zlraw", "events.jsonl", "checkpoint.json"]
    quality = json.loads((root / "recording.json").read_bytes()).get("record_quality", False)
    if quality:
        files.append("quality_model.jsonl")
    for name in files:
        baseline = sha256_file(root / "baseline" / name)
        assert sha256_file(root / "resumed" / name) == baseline
        assert sha256_file(root / "branch" / name) == baseline
    raw = scan_blocks(root / "branch/raw.zlraw", lambda *_: None)
    assert not raw.errors and not raw.losses and len(raw.blocks) == 2
    final = json.loads((root / "branch/run.json").read_text())
    assert final["status"] == "complete" and final["summary"]["closed"]
    assert final["summary"]["fine_step"] == "20000"
    paused = json.loads((root / "pause_receipt.json").read_text())
    assert paused["status"] == "paused" and paused["summary"]["fine_step"] == "10000"
    assert not paused["summary"]["closed"]
    killed = json.loads((root / "kill_receipt.json").read_text())
    assert killed["returncode"] != 0 and killed["unknown_tail_bytes"] > 0
    assert sha256_file(root / "fault/checkpoint.json") == killed["committed_checkpoint_sha256"]
    result = dict(
        raw_sha256=sha256_file(root / "branch/raw.zlraw"),
        checkpoint_sha256=sha256_file(root / "branch/checkpoint.json"),
        delivered_blocks=len(raw.blocks),
        fine_step=final["summary"]["fine_step"],
        resumed_equals_continuous=True,
        branch_equals_continuous=True,
        fault_source_unchanged=True,
        unknown_tail_preserved=True,
        process_kill_exit=killed["returncode"],
        origin="SIMULATED",
        scope="same-host public CLI and process kill; not power failure, browser or hardware",
    )
    if quality:
        assert killed["unknown_quality_tail_bytes"] > 0
        assert (root / "fault/quality_model.jsonl").stat().st_size == (
            killed["committed_quality_size"] + killed["unknown_quality_tail_bytes"]
        )
        from zlscan.engine.saved_service import saved_service

        with saved_service(root / "branch") as service:
            assert service.quality is not None
            actual = service.events(
                service.runs["recording-c0"], 0, 20000, {"adc_saturated"}, "research"
            )
            assert actual["status"] == "exact_record"
        result["quality_sha256"] = sha256_file(root / "branch/quality_model.jsonl")
    print("Public unknown-length recording pause/resume and process-kill branch passed")
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--out", type=Path)
    group.add_argument("--verify", type=Path)
    p.add_argument("--record-quality", action="store_true")
    args = p.parse_args()
    if args.verify:
        root = args.verify.resolve()
        assert verify(root) == json.loads((root / "audit.json").read_text())
        print("Fresh-process recording audit agrees exactly")
        return
    root = args.out.resolve()
    root.mkdir(parents=True, exist_ok=False)
    fixture = Path(__file__).resolve().parents[1] / "configs/physical_recording.json"
    config = root / "recording.json"
    if args.record_quality:
        data = json.loads(fixture.read_bytes())
        data["record_quality"] = True
        config.write_text(json.dumps(data))
    else:
        config.write_bytes(fixture.read_bytes())
    command_log = []

    def cli(label, extra, commands, expected=0):
        argv = [sys.executable, "-m", "zlscan", "run", "--mode", "recording", *extra]
        proc = subprocess.run(
            argv,
            input="".join(json.dumps(c) + "\n" for c in commands),
            capture_output=True,
            text=True,
            timeout=60,
        )
        (root / f"{label}.jsonl").write_text(proc.stdout)
        (root / f"{label}.stderr").write_text(proc.stderr)
        command_log.append(
            dict(label=label, argv=argv, expected=expected, returncode=proc.returncode)
        )
        (root / "commands.json").write_text(json.dumps(command_log, indent=2) + "\n")
        assert proc.returncode == expected, (label, proc.stderr)
        return [json.loads(line) for line in proc.stdout.splitlines()]

    def lines(n):
        return {"op": "lines", "count": n}

    finish = {"op": "finish"}
    cli("baseline", ["--config", str(config), "--out", str(root / "baseline")], [lines(2), finish])
    receipt = cli("pause", ["--config", str(config), "--out", str(root / "resumed")], [lines(1)])[
        -1
    ]
    (root / "pause_receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    cli("resume", ["--resume", str(root / "resumed")], [lines(1), finish])
    argv = [
        sys.executable,
        "-m",
        "zlscan",
        "run",
        "--mode",
        "recording",
        "--config",
        str(config),
        "--out",
        str(root / "fault"),
    ]
    proc = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    readers = []

    def ack():
        q = queue.Queue()
        thread = threading.Thread(target=lambda: q.put(proc.stdout.readline()), daemon=True)
        thread.start()
        readers.append(thread)
        value = q.get(timeout=30)
        thread.join(timeout=1)
        assert value, "fault producer exited before acknowledgement"
        return json.loads(value)

    try:
        assert ack()["op"] == "open"
        proc.stdin.write(json.dumps(lines(1)) + "\n")
        proc.stdin.flush()
        committed = ack()
        size = (root / "fault/raw.zlraw").stat().st_size
        checkpoint = sha256_file(root / "fault/checkpoint.json")
        assert committed["checkpoint_sha256"] == checkpoint
        proc.stdin.write(json.dumps(lines(100000)) + "\n")
        proc.stdin.flush()
        deadline = time.monotonic() + 20
        while (root / "fault/raw.zlraw").stat().st_size <= size:
            assert proc.poll() is None, "fault producer exited prematurely"
            assert time.monotonic() < deadline, "producer did not generate an uncommitted tail"
            time.sleep(0.01)
        proc.kill()
        stdout, stderr = proc.communicate(timeout=20)
        assert proc.returncode != 0
        assert sha256_file(root / "fault/checkpoint.json") == checkpoint
        (root / "fault.stdout").write_text(stdout)
        (root / "fault.stderr").write_text(stderr)
        receipt = dict(
            argv=argv,
            returncode=proc.returncode,
            committed_checkpoint_sha256=checkpoint,
            committed_raw_size=size,
            unknown_tail_bytes=(root / "fault/raw.zlraw").stat().st_size - size,
        )
        if args.record_quality:
            paired = json.loads((root / "fault/checkpoint.json").read_bytes())["quality"]
            receipt["committed_quality_size"] = int(paired["file"]["offset"])
            receipt["unknown_quality_tail_bytes"] = (
                root / "fault/quality_model.jsonl"
            ).stat().st_size - receipt["committed_quality_size"]
            assert receipt["unknown_quality_tail_bytes"] > 0
        (root / "kill_receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=20)
        for reader in readers:
            reader.join(timeout=1)
    (root / "fault_manifest.json").write_text(json.dumps(hashes(root / "fault"), indent=2) + "\n")
    cli("tail_refused", ["--resume", str(root / "fault")], [lines(1)], 1)
    assert hashes(root / "fault") == json.loads((root / "fault_manifest.json").read_text())
    cli(
        "branch",
        ["--resume", str(root / "fault"), "--out", str(root / "branch")],
        [lines(1), finish],
    )
    (root / "audit.json").write_text(json.dumps(verify(root), indent=2) + "\n")


if __name__ == "__main__":
    main()
