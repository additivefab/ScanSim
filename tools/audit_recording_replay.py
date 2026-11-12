"""Public controlled recording replay, independent Chain, delivered-code and refusal audit."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np

from zlscan.engine.chain import Chain
from zlscan.engine.physical_recording import PhysicalRecorder
from zlscan.engine.recording_replay import RecordingReplay
from zlscan.io.manifest import sha256_file
from zlscan.io.rawstream import PRELUDE, read_all


def hashes(path):
    return {p.relative_to(path).as_posix(): sha256_file(p) for p in path.rglob("*") if p.is_file()}


def verify(root):
    original = json.loads((root / "source_manifest.json").read_text())
    assert hashes(root / "source") == original
    with np.load(root / "replay/samples.npz", allow_pickle=False) as file:
        data = {key: file[key] for key in file.files}
    states = json.loads((root / "replay/final_states.json").read_text())
    metadata = json.loads((root / "replay/run.json").read_text())
    assert metadata["status"] == "replayed" and metadata["visibility"] == "ORACLE_ONLY"
    assert metadata["checkpoint_step"] == 10000 and data["codes"].shape == (200, 2)
    assert data["line"].dtype == np.int64
    with RecordingReplay(root / "source") as saved:
        for channel, cfg in saved.configs.items():
            chain = Chain(replace(cfg))
            pieces = []
            for end in [10321, 10407, 11000]:
                pieces.append(chain.advance(end - chain.step))
                if end == 10321 and channel == 0:
                    chain.cfg.source = replace(chain.cfg.source, power_w=0.0002)
                if end == 10407 and channel == 1:
                    chain.cfg.detector = replace(chain.cfg.detector, gain=2400.0)
            codes = np.concatenate([part.codes for part in pieces])
            assert np.array_equal(data["codes"][:, channel], codes[2550:2750])
            assert json.loads(states[str(channel)]) == json.loads(chain.checkpoint())
            final = saved.root["chains"][str(channel)]
            assert json.loads(states[str(channel)]) == {**final, "step": int(final["step"])}
    observed = 0
    raw = read_all(root / "source/raw.zlraw")
    assert not raw.errors and not raw.losses
    for segment in raw.segments:
        lo = max(2550, segment.first_sample_index)
        hi = min(2750, segment.first_sample_index + len(segment.samples))
        if hi > lo:
            assert np.array_equal(
                data["codes"][lo - 2550 : hi - 2550],
                segment.samples[lo - segment.first_sample_index : hi - segment.first_sample_index],
            )
            assert np.all(data["epochs"][lo - 2550 : hi - 2550] == segment.epoch)
            observed += hi - lo
    assert observed > 0
    for name in ["repeat", "tail_replay"]:
        with np.load(root / name / "samples.npz", allow_pickle=False) as other:
            assert set(other.files) == set(data)
            assert all(np.array_equal(data[key], other[key]) for key in data)
        assert json.loads((root / name / "final_states.json").read_text()) == states
    tail_meta = json.loads((root / "tail_replay/run.json").read_text())
    assert tail_meta["uncommitted_tail_present"]
    assert hashes(root / "tail") == json.loads((root / "tail_manifest.json").read_text())
    assert hashes(root / "forked_history") == json.loads((root / "fork_manifest.json").read_text())
    assert not (root / "legacy_refused").exists() and not (root / "fork_refused").exists()
    assert "no recorded checkpoint" in (root / "legacy.stderr").read_text()
    assert "not a prefix" in (root / "fork.stderr").read_text()
    assert "prefix" in (root / "truncated.stderr").read_text()
    assert not (root / "truncated_refused").exists()
    assert hashes(root / "truncated_cursor") == json.loads(
        (root / "truncated_manifest.json").read_text()
    )
    assert hashes(root / "source") == original
    sizes = [p.stat().st_size for p in (root / "source/history").glob("*.json")]
    result = dict(
        channels=metadata["channels"],
        checkpoint_step=10000,
        steps=800,
        samples_per_channel=200,
        history_points=len(sizes),
        checkpoint_bytes_min=min(sizes),
        checkpoint_bytes_max=max(sizes),
        overlapping_delivered_rows=observed,
        epochs=sorted(int(e) for e in np.unique(data["epochs"])),
        arrays={
            key: dict(
                shape=list(value.shape),
                dtype=str(value.dtype),
                sha256=hashlib.sha256(value.tobytes()).hexdigest(),
            )
            for key, value in data.items()
        },
        source_root_sha256=metadata["source_root_sha256"],
        independent_chain_equal=True,
        source_final_states_equal=True,
        delivered_rows_equal=True,
        source_unchanged=True,
        unknown_tails_preserved=True,
        incompatible_history_refused=True,
        rehashed_truncated_prefix_refused=True,
        visibility="ORACLE_ONLY",
        origin="SIMULATED",
        scope="public CLI model replay, not browser or hardware/statistical acceptance",
    )
    print("Recording replay matches independent chains, committed final states and delivered rows")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--out", type=Path)
    group.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if args.verify:
        root = args.verify.resolve()
        assert verify(root) == json.loads((root / "audit.json").read_text())
        print("Fresh-process recording replay checks agree exactly")
        return
    root = args.out.resolve()
    root.mkdir(parents=True, exist_ok=False)
    fixture = Path(__file__).resolve().parents[1] / "configs/physical_recording.json"
    config = json.loads(fixture.read_text())
    config["chain"]["source"]["gated"] = False
    config["chain"]["detector"].update(shot_noise=True, pole_hz=70000.0)
    config["chain"]["psf"].update(nodes_x=1, nodes_y=1)
    config["chunk_steps"] = 97
    config["acquisition"] = {"window_start": 90, "window_length": 100}
    config_path = root / "config.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    commands = [
        {"op": "lines", "count": 1},
        {
            "op": "request",
            "channel": 0,
            "parameter": "source.power_w",
            "value": 0.0002,
            "effective_step": 10321,
        },
        {
            "op": "request",
            "channel": 1,
            "parameter": "detector.gain",
            "value": 2400.0,
            "effective_step": 10407,
        },
        {"op": "sync", "ticks": ["2500007000"]},
        {"op": "advance", "steps": 450},
        {"op": "reset"},
        {"op": "start"},
        {"op": "advance", "steps": 550},
        {"op": "finish"},
    ]
    ledger = []

    def run(label, argv, expected=0):
        command = [sys.executable, "-m", "zlscan", *argv]
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        (root / f"{label}.stdout").write_text(result.stdout)
        (root / f"{label}.stderr").write_text(result.stderr)
        ledger.append(
            dict(label=label, argv=command, expected=expected, returncode=result.returncode)
        )
        (root / "commands.json").write_text(json.dumps(ledger, indent=2) + "\n")
        assert result.returncode == expected, (label, result.stdout, result.stderr)

    command_path = root / "commands.jsonl"
    command_path.write_text("".join(json.dumps(c) + "\n" for c in commands))
    run(
        "producer",
        [
            "run",
            "--mode",
            "recording",
            "--config",
            str(config_path),
            "--commands",
            str(command_path),
            "--out",
            str(root / "source"),
        ],
    )
    (root / "source_manifest.json").write_text(json.dumps(hashes(root / "source"), indent=2) + "\n")

    def replay(label, source, output, expected=0):
        run(
            label,
            [
                "replay",
                "--run",
                str(source),
                "--start-step",
                "10200",
                "--steps",
                "800",
                "--probes",
                "power_w,current_a,v_adc,line",
                "--out",
                str(output),
            ],
            expected,
        )

    replay("replay", root / "source", root / "replay")
    replay("repeat", root / "source", root / "repeat")
    shutil.copytree(root / "source", root / "tail")
    for name in ["raw.zlraw", "events.jsonl"]:
        with (root / "tail" / name).open("ab") as file:
            file.write(b"unknown uncommitted tail")
    (root / "tail_manifest.json").write_text(json.dumps(hashes(root / "tail"), indent=2) + "\n")
    replay("tail", root / "tail", root / "tail_replay")
    shutil.copytree(root / "source", root / "legacy", ignore=shutil.ignore_patterns("history"))
    replay("legacy", root / "legacy", root / "legacy_refused", 1)
    commands[1]["value"] = 0.0003
    command_path = root / "alternate_commands.jsonl"
    command_path.write_text("".join(json.dumps(c) + "\n" for c in commands))
    run(
        "alternate_producer",
        [
            "run",
            "--mode",
            "recording",
            "--config",
            str(config_path),
            "--commands",
            str(command_path),
            "--out",
            str(root / "alternate"),
        ],
    )
    shutil.copytree(root / "source", root / "forked_history")
    alternate = next(
        p
        for p in (root / "alternate/history").glob("*.json")
        if len(json.loads(p.read_text())["commands"]) == 1
    )
    shutil.copyfile(alternate, root / "forked_history/history" / alternate.name)
    (root / "fork_manifest.json").write_text(
        json.dumps(hashes(root / "forked_history"), indent=2) + "\n"
    )
    replay("fork", root / "forked_history", root / "fork_refused", 1)
    truncated = root / "truncated_cursor"
    shutil.copytree(root / "source", truncated)
    raw = (truncated / "raw.zlraw").read_bytes()
    length = struct.unpack("<I", raw[len(PRELUDE) : len(PRELUDE) + 4])[0]
    limit = length + 5  # Seven session JSON bytes lie beyond this deliberately invalid cursor.
    document = json.loads((truncated / "checkpoint.json").read_text())
    writer = document["recording"]["writer"]
    writer["file"] = {
        "offset": str(limit),
        "prefix_sha256": hashlib.sha256(raw[:limit]).hexdigest(),
    }
    for component in [writer, document["recording"], document]:
        component["checksum"] = PhysicalRecorder._hash(
            {k: v for k, v in component.items() if k != "checksum"}
        )
    (truncated / "checkpoint.json").write_text(json.dumps(document, sort_keys=True))
    (root / "truncated_manifest.json").write_text(json.dumps(hashes(truncated), indent=2) + "\n")
    replay("truncated", truncated, root / "truncated_refused", 1)
    (root / "audit.json").write_text(json.dumps(verify(root), indent=2) + "\n")


if __name__ == "__main__":
    main()
