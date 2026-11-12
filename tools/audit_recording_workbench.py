"""Audit public recording CLI/HTTP, exact fresh-process queries and source preservation."""

from __future__ import annotations

import argparse
import http.client
import json
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from zlscan.engine.recording_replay import RecordingReplay
from zlscan.io.manifest import sha256_file
from zlscan.io.rawstream import read_all


def hashes(path: Path) -> dict:
    return {p.relative_to(path).as_posix(): sha256_file(p) for p in path.rglob("*") if p.is_file()}


def verify(root: Path) -> dict:
    source = root / "source"
    before = json.loads((root / "source_manifest.json").read_text())
    assert hashes(source) == before
    log = root / "server.log"
    result = {}
    with log.open("w") as stream:
        process = subprocess.Popen(
            [sys.executable, "-m", "zlscan", "serve", "--run", str(source), "--port", "0"],
            stdout=stream,
            stderr=stream,
        )
        try:
            deadline = time.monotonic() + 30
            port = None
            while time.monotonic() < deadline:
                match = re.search(r"http://127\.0\.0\.1:(\d+)/", log.read_text())
                if match:
                    port = int(match.group(1))
                    break
                if process.poll() is not None:
                    raise RuntimeError("recording server exited; see server.log")
                time.sleep(0.05)
            assert port is not None, "server startup timeout"

            def query(path, body=None):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
                try:
                    connection.request(
                        "GET" if body is None else "POST",
                        path,
                        body=None if body is None else json.dumps(body),
                    )
                    response = connection.getresponse()
                    assert response.status == 200, (path, response.status)
                    value = json.loads(response.read())
                    result[path] = value
                    return value
                finally:
                    connection.close()

            assert set(query("/runs")["runs"]) == {"recording-c0", "recording-c1"}
            raw = read_all(source / "raw.zlraw")
            with RecordingReplay(source) as model:
                expected = model.replay(10200, 800, {"power_w"})
            for channel in [0, 1]:
                prefix = f"/runs/recording-c{channel}"
                manifest = query(prefix + "/manifest")
                assert manifest["frozen_commit"] and manifest["read_only"]
                assert not manifest["live_follow"] and manifest["steps"] == 11000
                query(prefix + "/signals?view=instrument")
                query(prefix + "/signals?view=research")
                delivered = query(
                    prefix + "/traces?signal=adc.code&start=10200&end=11000"
                    "&pixel_width=400&view=instrument"
                )
                values = [None] * 200
                for segment in raw.segments:
                    for offset, row in enumerate(segment.samples):
                        pos = segment.first_sample_index + offset - 2550
                        if 0 <= pos < 200:
                            values[pos] = int(row[channel])
                assert delivered["values"] == values and delivered["missing_count"] > 0
                assert delivered["visibility"] == "OBSERVABLE"
                coarse = query(
                    prefix
                    + "/traces?signal=adc.code&start=10200&end=11000&pixel_width=1&view=instrument"
                )
                assert coarse["mixed_epoch"] == [True] and coarse["epochs"] == [None]
                research = query(
                    prefix + "/traces?signal=pmt.power_w&start=10200&end=11000"
                    "&pixel_width=800&view=research"
                )
                assert research["values"] == expected["power_w"][:, channel].tolist()
                assert research["visibility"] == "ORACLE_ONLY"
                replay = query(
                    prefix + "/replay-window?view=research",
                    dict(start=10200, n_steps=800, probes=["power_w"]),
                )
                assert replay["codes"] == expected["codes"][:, channel].tolist()
                assert replay["epochs"] == expected["epochs"].tolist()
                assert (
                    query(prefix + "/state?step=10321&probes=power_w&view=research")["status"]
                    == "replayed"
                )
                assert (
                    query(prefix + "/events?start=0&end=11000&view=instrument")["status"]
                    == "not_recorded"
                )
                controls = query(
                    prefix + "/events?start=0&end=11000&kinds=control_effective&view=research"
                )
                assert controls["events"][0]["step"] == 10321
                i = raw.blocks[-1].first_sample_index
                observable = query(prefix + f"/samples/{i}/lineage?view=instrument")
                assert observable["lineage"]["capture_time"] is not None
                assert observable["lineage"]["host_arrival_time"] is None
                assert (
                    "model_lineage" not in observable and "transport_diagnostic" not in observable
                )
                lineage = query(prefix + f"/samples/{i}/lineage?view=research")
                assert lineage["model_lineage"]["derivation"] == "MODEL_ESTIMATE"
                assert lineage["transport_diagnostic"]["host_arrival_time"] is None
                assert (
                    query(prefix + "/samples/0/lineage?view=instrument")["status"] == "not_recorded"
                )
                assert (
                    query(
                        prefix + "/replay-window?view=instrument", dict(start=10200, n_steps=100)
                    )["status"]
                    == "not_computed"
                )
                assert query(prefix + "/branches", {})["status"] == "not_computed"
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
            assert process.wait(timeout=20) in (0, 130, -signal.SIGINT)
    assert hashes(source) == before
    print("Recording CLI/HTTP passed; both channels/gaps/epochs/replay verified; source unchanged")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--out", type=Path)
    group.add_argument("--verify", type=Path)
    args = parser.parse_args()
    if args.out:
        root = args.out.resolve()
        root.mkdir(parents=True, exist_ok=False)
        config = dict(
            format="zlscan.physical-recording/1",
            model_version="chain-0.3",
            preset="nominal",
            chain={
                "seed": 3,
                "source": {"gated": False},
                "detector": {"shot_noise": False},
                "psf": {"nodes_x": 1, "nodes_y": 1},
            },
            secondary={"seed": 31},
            link={"latency_half_cycles": 25, "propagation_ps": 1500},
            acquisition={"window_start": 90, "window_length": 100},
            stages=[{"name": "disk", "capacity_bytes": 4096, "rate_Bps": 1e9}],
            session_id=19,
            run_id="physical",
            branch_id="main",
            chunk_steps=97,
        )
        (root / "config.json").write_text(json.dumps(config))
        commands = [
            {"op": "lines", "count": 1},
            {
                "op": "request",
                "channel": 0,
                "parameter": "source.power_w",
                "value": 0.0002,
                "effective_step": 10321,
            },
            {"op": "sync", "ticks": ["2500007000"]},
            {"op": "advance", "steps": 450},
            {"op": "reset"},
            {"op": "start"},
            {"op": "advance", "steps": 550},
            {"op": "finish"},
        ]
        (root / "commands.jsonl").write_text("".join(json.dumps(c) + "\n" for c in commands))
        run = subprocess.run(
            [
                sys.executable,
                "-m",
                "zlscan",
                "run",
                "--mode",
                "recording",
                "--config",
                str(root / "config.json"),
                "--commands",
                str(root / "commands.jsonl"),
                "--out",
                str(root / "source"),
            ],
            capture_output=True,
            text=True,
        )
        (root / "producer.log").write_text(run.stdout + run.stderr)
        assert run.returncode == 0, run.stderr
        (root / "source_manifest.json").write_text(
            json.dumps(hashes(root / "source"), indent=2) + "\n"
        )
        report = verify(root)
        (root / "audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    else:
        expected = json.loads((args.verify / "audit.json").read_text())
        assert verify(args.verify.resolve()) == expected, "fresh-process queries differ"
        print("Fresh-process HTTP responses agree exactly")


if __name__ == "__main__":
    main()
