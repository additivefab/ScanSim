"""Fresh-process ADC link checkpoint audit; assumed functional stimulus, no hardware claim."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np

from zlscan.adc.patterns import ConvertedSamples
from zlscan.adc_link.link import LinkConfig, receive, transmit
from zlscan.adc_link.stream import TIMES, StreamLink
from zlscan.contracts.time import ClockDomain

SEED = 20261112


def rows(outputs):
    result = []
    for output in outputs:
        recovered = receive(output, output.config)
        for index in range(len(output.words)):
            result.append(
                {
                    "sample_index": str(output.first_index + index),
                    "epoch": output.epoch,
                    "run_id": output.run_id,
                    "branch_id": output.branch_id,
                    "session_id": output.session_id,
                    "channels": list(output.channels),
                    "words": output.words[index].tolist(),
                    "recovered": recovered.codes[index].tolist(),
                    "overflow": bool(output.overflow[index]),
                    "overflow_valid": bool(output.overflow_valid[index]),
                    **{key: str(int(getattr(output, key)[index])) for key in TIMES},
                }
            )
    return result


def child(specification: Path, checkpoint: Path, output: Path):
    spec = json.loads(specification.read_text())
    domain, config = ClockDomain(**spec["domain"]), LinkConfig(**spec["config"])
    channels = tuple(spec["channels"])
    for key in ("bit_map", "channel_order"):
        if getattr(config, key) is not None:
            config = replace(config, **{key: tuple(getattr(config, key))})
    codes = np.load(specification.parent / "codes.npy", allow_pickle=False)
    start = time.perf_counter_ns()
    engine = StreamLink.restore(domain, config, channels, checkpoint.read_bytes())
    source = ConvertedSamples(
        codes[17:],
        channels,
        spec["first_index"] + 17,
        5,
        domain,
        run_id="audit",
        branch_id="main",
        session_id=19,
    )
    engine.push(source)
    engine.finish()
    engine.advance()
    elapsed = (time.perf_counter_ns() - start) / 1e6
    output.write_text(json.dumps({"rows": rows(engine.drain()), "resume_ms": elapsed}))


def audit(directory: Path):
    directory.mkdir(parents=True, exist_ok=False)
    domain = ClockDomain(7, 125_000_003, phase_ticks=2**53 + 17)
    codes = np.random.default_rng(SEED).integers(1, 65535, (64, 2), dtype=np.uint16)
    codes[[0, 5, 15, 16, 17, 50, 63], 0] = [0, 65535, 0, 65535, 0, 65535, 0]
    np.save(directory / "codes.npy", codes)
    source = ConvertedSamples(
        codes, (0, 1), 2**32 - 5, 4, domain, run_id="audit", branch_id="main", session_id=19
    )
    cases = []
    for edge in ("rising", "falling"):
        for shift in (-5, 0, 5):
            config = LinkConfig(
                coding="twos_complement",
                bit_map=tuple(reversed(range(16))),
                channel_order=(1, 0),
                latency_half_cycles=25,
                propagation_ps=1500,
                capture_edge=edge,
                overflow_offset_cycles=shift,
            )
            engine = StreamLink(domain, config, source.channels)
            engine.push(replace(source, codes=codes[:17]))
            engine.advance(source.sample_time(16))
            prefix = rows(engine.drain()) if shift == 0 else []
            checkpoint = directory / f"state_{edge}_{shift}.json"
            start = time.perf_counter_ns()
            checkpoint.write_bytes(engine.checkpoint())
            save_ms = (time.perf_counter_ns() - start) / 1e6
            assert checkpoint.read_bytes() == engine.checkpoint()
            spec = directory / f"spec_{edge}_{shift}.json"
            spec.write_text(
                json.dumps(
                    {
                        "domain": asdict(domain),
                        "config": asdict(config),
                        "channels": source.channels,
                        "first_index": source.first_index,
                    }
                )
            )
            output = directory / f"resumed_{edge}_{shift}.json"
            command = [
                sys.executable,
                __file__,
                "--resume",
                str(checkpoint),
                "--spec",
                str(spec),
                "--out",
                str(output),
            ]
            completed = subprocess.run(command, capture_output=True, text=True, timeout=30)
            assert completed.returncode == 0, completed.stdout + completed.stderr
            resumed = json.loads(output.read_text())
            expected = rows([transmit(source, config)])
            for index, row in enumerate(expected):
                row["epoch"] = 4 if index < 17 else 5
                assert row["recovered"] == codes[index].tolist()
            assert prefix + resumed["rows"] == expected
            cases.append(
                {
                    "edge": edge,
                    "overflow_shift": shift,
                    "pending_samples": engine.pending_count,
                    "unread_samples": sum(len(o.words) for o in engine.ready()),
                    "checkpoint_bytes": checkpoint.stat().st_size,
                    "serialize_write_ms": save_ms,
                    "resume_remaining_ms": resumed["resume_ms"],
                    "samples_verified": len(expected),
                    "all_words_times_flags_identities_match": True,
                    "child_command": command,
                    "child_returncode": completed.returncode,
                }
            )
    report = {
        "seed": SEED,
        "cases": cases,
        "passed": True,
        "scope": (
            "ADC link only; synthetic assumed clock/delays; no acquisition/control/writer state"
        ),
        "timing_scope": (
            "Save+write; read+restore+47 remaining inputs+pipeline drain. "
            "Excludes process startup and output JSON. "
            "Disk cache uncontrolled; no fsync."
        ),
    }
    (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--spec", type=Path)
    args = parser.parse_args()
    if args.resume:
        child(args.spec, args.resume, args.out)
    else:
        report = audit(args.out)
        print(f"{len(report['cases'])} ADC pipeline file checkpoints matched in fresh processes")
