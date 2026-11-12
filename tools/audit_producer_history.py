"""Public CLI, independent original-batch replay, restart and read-only history audit."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np

from zlscan.adc.patterns import ConvertedSamples
from zlscan.engine.chain import Chain
from zlscan.engine.digital_pipeline import DigitalPipeline
from zlscan.engine.physical_recording import PhysicalRecorder
from zlscan.engine.recording_replay import RecordingReplay
from zlscan.io.manifest import sha256_file


def hashes(path):
    return {p.relative_to(path).as_posix(): sha256_file(p) for p in path.rglob("*") if p.is_file()}


def verify(root):
    source = root / "source"
    before = hashes(source)
    assert before == json.loads((root / "source_manifest.json").read_text())
    states = []
    with RecordingReplay(source) as saved:
        pipeline = DigitalPipeline(
            saved.current.domain,
            saved.link,
            saved.acquisition,
            saved.stages,
            saved.current.channels,
        )
        chains = {c: Chain(replace(cfg)) for c, cfg in saved.configs.items()}
        commands = saved.current.commands
        cursor = samples = 0
        for row in saved.history_reader.after(None):
            kind = row["op"]
            if kind == "sync":
                pipeline.schedule_syncs([int(t) for t in row["ticks"]])
            elif kind == "finish":
                pipeline.finish()
                pipeline.drain()
            else:
                step, end = int(row["step"]), int(row["end"])
                while cursor < len(commands) and int(commands[cursor]["effective_step"]) <= step:
                    command = commands[cursor]
                    PhysicalRecorder._apply_config(chains[command["channel"]].cfg, command)
                    cursor += 1
                assert cursor == len(commands) or int(commands[cursor]["effective_step"]) >= end
                outputs = [chain.advance(end - step) for chain in chains.values()]
                codes = np.column_stack([out.codes for out in outputs])
                first = outputs[0].first_sample
                for part in row["sources"]:
                    begin, count = int(part["first_index"]), part["count"]
                    words = codes[begin - first : begin - first + count]
                    actual = hashlib.sha256(
                        np.ascontiguousarray(words, dtype="<u2").tobytes()
                    ).hexdigest()
                    assert actual == part["codes_sha256"]
                    assert all(
                        saved.current.controller.epoch_at(saved.current.domain.tick_of(i)).id
                        == part["epoch"]
                        for i in range(begin, begin + count)
                    )
                    pipeline.push(
                        ConvertedSamples(
                            words,
                            saved.current.channels,
                            begin,
                            part["epoch"],
                            saved.current.domain,
                            saved.link.bits,
                            **saved.current.identity,
                        )
                    )
                    pipeline.drain()
                    samples += count
            actual = hashlib.sha256(pipeline.checkpoint()).hexdigest()
            assert actual == row["after"]
            states.append(dict(op=kind, step=row["step"], pipeline_sha256=actual))
        assert samples > 0
        for channel, chain in chains.items():
            assert chain.checkpoint() == saved.current.chains[channel].checkpoint()
        replay = saved.replay(0, saved.step, set())
        assert replay["codes"].shape[0] == samples
    for name in ("resumed", "branched"):
        for filename in ("raw.zlraw", "events.jsonl", "producer_history.jsonl", "checkpoint.json"):
            assert (root / name / filename).read_bytes() == (source / filename).read_bytes()
    assert hashes(source) == before
    assert hashes(root / "tail") == json.loads((root / "tail_manifest.json").read_text())
    return dict(
        origin="SIMULATED",
        visibility="ORACLE_ONLY",
        source_samples=samples,
        verified_operations=states,
        source_files_unchanged=len(before),
        restart_and_branch_bytes_equal=True,
        scope="Software replay and process restart; no browser, power-loss or hardware proof",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    root = args.out.resolve()
    if args.verify:
        assert verify(root) == json.loads((root / "audit.json").read_text())
        print("fresh-process original operations and source immutability verified")
        return
    root.mkdir(parents=True, exist_ok=False)
    config = json.loads(
        (Path(__file__).resolve().parents[1] / "configs/physical_recording.json").read_text()
    )
    config["record_digital_history"] = True
    config["chunk_steps"] = 97
    config["chain"]["detector"].update(shot_noise=True, pole_hz=70000.0)
    config["chain"]["psf"].update(nodes_x=1, nodes_y=1)
    config["acquisition"] = dict(window_start=2, window_length=13)
    (root / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    first = [
        dict(op="sync", ticks=["20000000", "50000000"]),
        dict(op="advance", steps=57),
        dict(op="request", channel=0, parameter="detector.gain", value=2400.0, effective_step=113),
        dict(op="advance", steps=80),
    ]
    last = [dict(op="advance", steps=160), dict(op="finish")]
    receipts = []

    def run(label, arguments, rows, expected=0):
        command_path = root / f"{label}_commands.jsonl"
        command_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        argv = [
            sys.executable,
            "-m",
            "zlscan",
            "run",
            "--mode",
            "recording",
            *arguments,
            "--commands",
            str(command_path),
        ]
        result = subprocess.run(argv, capture_output=True, text=True)
        (root / f"{label}.stdout").write_text(result.stdout)
        (root / f"{label}.stderr").write_text(result.stderr)
        receipts.append(dict(argv=argv, returncode=result.returncode, expected_returncode=expected))
        (root / "commands.json").write_text(json.dumps(receipts, indent=2) + "\n")
        assert result.returncode == expected, result.stderr

    run(
        "continuous",
        ["--config", str(root / "config.json"), "--out", str(root / "source")],
        first + last,
    )
    run("paused", ["--config", str(root / "config.json"), "--out", str(root / "resumed")], first)
    run("resumed", ["--resume", str(root / "resumed"), "--out", str(root / "resumed")], last)
    (root / "source_manifest.json").write_text(json.dumps(hashes(root / "source"), indent=2) + "\n")
    shutil.copytree(root / "source", root / "tail")
    with (root / "tail/producer_history.jsonl").open("ab") as stream:
        stream.write(b"uncommitted incomplete producer operation")
    (root / "tail_manifest.json").write_text(json.dumps(hashes(root / "tail"), indent=2) + "\n")
    run(
        "tail_refused",
        ["--resume", str(root / "tail"), "--out", str(root / "tail")],
        [],
        expected=1,
    )
    run("branched", ["--resume", str(root / "tail"), "--out", str(root / "branched")], [])
    (root / "audit.json").write_text(json.dumps(verify(root), indent=2) + "\n")
    print("original producer operation audit passed")


if __name__ == "__main__":
    main()
