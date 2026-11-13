"""Actual recording CLI capture-v2 export, resume/branch equivalence and fresh readback.

All instrument settings are synthetic assumptions. This is a producer/interface audit, not
physical calibration, hardware or browser acceptance. No test fixtures are imported.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from zlscan.contracts.calibration import CaptureDescription
from zlscan.io.manifest import sha256_file
from zlscan.io.rawstream import scan_blocks


def verify(root):
    captures = []
    for name in ("baseline", "resumed", "branch"):
        directory = root / name
        capture = CaptureDescription.model_validate_json((directory / "capture.json").read_bytes())
        raw = scan_blocks(directory / "raw.zlraw", lambda *_: None)
        assert not raw.errors and raw.session is not None
        assert capture.raw_sha256 == sha256_file(directory / "raw.zlraw")
        assert capture.version == 2
        assert capture.lines == max(b.line_number for b in raw.blocks) + 1
        assert {e.epoch for e in capture.epoch_bindings} == {b.epoch for b in raw.blocks}
        assert capture.binding.channel_roles == raw.session.channel_roles
        assert capture.binding.simulation_hash is None
        bindings = {e.epoch: e.binding for e in capture.epoch_bindings}
        assert set(bindings) == {0, 1, 2}
        assert bindings[0].simulation_hash != bindings[1].simulation_hash
        assert bindings[1].simulation_hash == bindings[2].simulation_hash
        assert all(
            b.hardware_hash is None and b.register_dump_hash is None for b in bindings.values()
        )
        run = json.loads((directory / "run.json").read_bytes())
        assert run["status"] == "complete"
        assert run["capture_sha256"] == sha256_file(directory / "capture.json")
        captures.append((directory / "capture.json").read_bytes())
    assert captures[0] == captures[1] == captures[2]
    assert sha256_file(root / "resumed" / "checkpoint.json") == sha256_file(
        root / "baseline" / "checkpoint.json"
    )
    return dict(
        format="zlscan.recording-capture-audit/1",
        origin="SIMULATED",
        actual_measurements_obtained=False,
        capture_sha256=sha256_file(root / "baseline" / "capture.json"),
        raw_sha256=sha256_file(root / "baseline" / "raw.zlraw"),
        exported_epochs=[0, 1, 2],
        logical_reset_preserves_settings=True,
        continuous_resume_branch_identical=True,
        scope="public recording CLI and file readback; not physical accuracy",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    root = args.out.resolve()
    if args.verify:
        assert verify(root) == json.loads((root / "audit.json").read_bytes())
        print("Fresh-process capture export verification passed")
        return
    root.mkdir(parents=True, exist_ok=False)
    config = {
        "format": "zlscan.physical-recording/1",
        "model_version": "chain-0.3",
        "preset": "nominal",
        "chain": {
            "seed": 3,
            "detector": {"shot_noise": False},
            "psf": {"nodes_x": 1, "nodes_y": 1},
        },
        "secondary": {"seed": 31},
        "link": {"latency_half_cycles": 25, "propagation_ps": 1500, "capture_edge": "falling"},
        "acquisition": {"window_start": 932, "window_length": 637},
        "stages": [{"name": "disk", "capacity_bytes": 65536, "rate_Bps": 10000000.0}],
        "session_id": 19,
        "run_id": "export-audit",
        "branch_id": "main",
        "chunk_steps": 1024,
    }
    path = root / "recording.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    prefix = [
        {"op": "lines", "count": 1},
        {
            "op": "request",
            "channel": 0,
            "parameter": "source.power_w",
            "value": 0.0004,
            "effective_step": "10321",
        },
        {"op": "lines", "count": 1},
    ]
    suffix = [
        {"op": "lines", "count": 1},
        {"op": "reset"},
        {"op": "start"},
        {"op": "lines", "count": 1},
        {"op": "finish"},
    ]

    def cli(name, options, commands):
        argv = [
            sys.executable,
            "-I",
            "-X",
            "utf8",
            "-m",
            "zlscan",
            "run",
            "--mode",
            "recording",
            *options,
        ]
        proc = subprocess.run(
            argv,
            input="".join(json.dumps(c) + "\n" for c in commands),
            text=True,
            encoding="utf-8",
            capture_output=True,
        )
        (root / (name + ".stdout")).write_text(proc.stdout, encoding="utf-8")
        (root / (name + ".stderr")).write_text(proc.stderr, encoding="utf-8")
        assert proc.returncode == 0, proc.stderr

    cli("baseline", ["--config", str(path), "--out", str(root / "baseline")], prefix + suffix)
    cli("pause", ["--config", str(path), "--out", str(root / "resumed")], prefix)
    paused_raw_sha = sha256_file(root / "resumed" / "raw.zlraw")
    cli("branch", ["--resume", str(root / "resumed"), "--out", str(root / "branch")], suffix)
    assert sha256_file(root / "resumed" / "raw.zlraw") == paused_raw_sha
    cli("resume", ["--resume", str(root / "resumed")], suffix)
    (root / "audit.json").write_text(json.dumps(verify(root), indent=2) + "\n", encoding="utf-8")
    print("Recording capture export audit passed")


if __name__ == "__main__":
    main()
