"""Restore a composed digital checkpoint in a fresh process and compare actual ZLRAW bytes."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np

from zlscan.adc.patterns import ConvertedSamples
from zlscan.adc_link.link import LinkConfig
from zlscan.contracts.time import CaptureClock, ClockDomain
from zlscan.digital.acquisition import AcquisitionConfig, channel_mask
from zlscan.engine.digital_pipeline import DigitalPipeline
from zlscan.io.manifest import sha256_file
from zlscan.io.rawstream import FLAG_EPOCH_START, RawWriter, read_all
from zlscan.transport.queues import StageSpec

SEED = 20261112
DOMAIN = ClockDomain(7, 125_000_003, phase_ticks=2**53 + 17)
FIRST = 2**32 - 5
ACQUISITION = AcquisitionConfig(2, 7, sync_latency_cycles=2)


def setup(edge, stalled):
    link = LinkConfig(
        latency_half_cycles=25,
        propagation_ps=1500,
        capture_edge=edge,
        coding="twos_complement",
        bit_map=tuple(reversed(range(16))),
        channel_order=(1, 0),
        overflow_offset_cycles=-5,
    )
    clock = CaptureClock(DOMAIN, edge=edge)
    stages = [
        StageSpec("rx", 184, 2e9),
        StageSpec(
            "disk",
            92,
            1e9 if stalled else 1e12,
            stalls=((clock.tick_of(FIRST + 30), 500_000),) if stalled else (),
        ),
    ]
    return link, stages, clock


def block(codes, start, end):
    return ConvertedSamples(
        codes[start:end],
        (0, 1),
        FIRST + start,
        4 if start < 25 else 5,
        DOMAIN,
        run_id="audit",
        branch_id="main",
        session_id=19,
    )


def write_result(engine, output):
    batch = engine.drain()
    raw = output.with_suffix(".zlraw")
    previous_epoch = None
    with RawWriter(raw, engine.session_header(19)) as writer:
        for row in batch.delivered:
            packet, source = row.packet, row.packet.source
            writer.write_block(
                epoch=source.epoch,
                channel_mask=channel_mask(source.channels),
                clock_domain=DOMAIN.id,
                first_sample_index=source.first_index,
                samples=source.codes,
                line_number=packet.item.line_number,
                flags=packet.flags | (FLAG_EPOCH_START if source.epoch != previous_epoch else 0),
            )
            previous_epoch = source.epoch
    verified = read_all(raw)
    assert not verified.errors and not verified.fatal
    result = {
        "delivered": [
            {"packet": row.packet.to_dict(), "delivery_tick": str(row.delivery_tick)}
            for row in batch.delivered
        ],
        "losses": [asdict(loss) for loss in batch.losses],
        "ignored_syncs": [str(tick) for tick in batch.ignored_syncs],
        "skipped_lines": [[str(line), reason] for line, reason in batch.skipped_lines],
        "stats": [asdict(stat) for stat in batch.stats],
    }
    output.write_text(json.dumps(result, indent=2) + "\n")
    return raw, result


def child(spec_path, checkpoint, output):
    spec = json.loads(spec_path.read_text())
    link, stages, _ = setup(spec["edge"], spec["stalled"])
    codes = np.load(spec_path.parent / "codes.npy", allow_pickle=False)
    start = time.perf_counter_ns()
    engine = DigitalPipeline.restore(
        DOMAIN, link, ACQUISITION, stages, (0, 1), checkpoint.read_bytes()
    )
    engine.push(block(codes, 38, 73))
    engine.finish()
    elapsed = (time.perf_counter_ns() - start) / 1e6
    write_result(engine, output)
    output.with_suffix(".timing.json").write_text(json.dumps({"resume_ms": elapsed}))


def audit(directory):
    directory.mkdir(parents=True, exist_ok=False)
    codes = np.random.default_rng(SEED).integers(1, 65535, (73, 2), dtype=np.uint16)
    codes[[3, 24, 25, 57, 63], 0] = [0, 65535, 0, 65535, 0]
    np.save(directory / "codes.npy", codes)
    cases = []
    for edge in ("rising", "falling"):
        for stalled in (False, True):
            label = f"{edge}_{int(stalled)}"
            link, stages, clock = setup(edge, stalled)

            def create(link=link, stages=stages, clock=clock):
                engine = DigitalPipeline(DOMAIN, link, ACQUISITION, stages, (0, 1))
                engine.schedule_syncs(
                    [clock.tick_of(FIRST + offset) for offset in (12, 30, 48, 66)]
                )
                return engine

            continuous = create()
            continuous.push(block(codes, 0, 25))
            continuous.push(block(codes, 25, 73))
            continuous.finish()
            baseline, _ = write_result(continuous, directory / f"continuous_{label}.json")
            engine = create()
            engine.push(block(codes, 0, 25))
            engine.push(block(codes, 25, 38))
            checkpoint = directory / f"checkpoint_{label}.json"
            start = time.perf_counter_ns()
            checkpoint.write_bytes(engine.checkpoint())
            save_ms = (time.perf_counter_ns() - start) / 1e6
            assert checkpoint.read_bytes() == engine.checkpoint()
            state = json.loads(checkpoint.read_bytes())["state"]
            spec = directory / f"spec_{label}.json"
            spec.write_text(json.dumps({"edge": edge, "stalled": stalled}))
            output = directory / f"resumed_{label}.json"
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
            result = json.loads(output.read_text())
            assert result == json.loads((directory / f"continuous_{label}.json").read_text())
            assert output.with_suffix(".zlraw").read_bytes() == baseline.read_bytes()
            cases.append(
                {
                    "edge": edge,
                    "stalled": stalled,
                    "checkpoint_bytes": checkpoint.stat().st_size,
                    "serialize_write_ms": save_ms,
                    "resume_ms": json.loads(output.with_suffix(".timing.json").read_text())[
                        "resume_ms"
                    ],
                    "pending_payload_packets": len(state["pending_packets"]),
                    "active_window_samples": sum(
                        len(segment["codes"])
                        for segment in state["acquisition"]["state"]["active"]["segments"]
                    ),
                    "delivered_packets": len(result["delivered"]),
                    "lost_packets": len(result["losses"]),
                    "actual_raw_bytes": baseline.stat().st_size,
                    "raw_sha256": sha256_file(baseline),
                    "raw_and_full_ledger_match": True,
                    "child_command": command,
                    "child_returncode": completed.returncode,
                }
            )
    report = {
        "seed": SEED,
        "passed": True,
        "cases": cases,
        "scope": (
            "Functional ADC+capture+queue+pending payloads; assumed clocks/rates. "
            "Excludes analog, controller and committed writer cursor."
        ),
        "timing_scope": (
            "Save+write and read+restore+35 inputs+EOF+queue drain. "
            "Excludes process startup, output serialization/writing. "
            "Cache uncontrolled; no fsync."
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
        print(f"{len(report['cases'])} composed digital checkpoints matched complete ZLRAW bytes")
