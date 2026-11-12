"""Real CLI/HTTP digital cursor audit with chronological reference and fresh-process readback."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import numpy as np

from zlscan.adc.patterns import ConvertedSamples
from zlscan.api.server import make_server
from zlscan.control.state import split_by_epoch
from zlscan.engine.chain import Chain
from zlscan.engine.digital_pipeline import DigitalPipeline
from zlscan.engine.saved_service import saved_service
from zlscan.io.manifest import sha256_file


def hashes(path):
    return {p.relative_to(path).as_posix(): sha256_file(p) for p in path.rglob("*") if p.is_file()}


def query(root, verify=False):
    folder = root / "source"
    before = hashes(folder)
    assert before == json.loads((root / "source_manifest.json").read_text())
    responses, elapsed = [], []
    with saved_service(folder) as service:
        server = make_server(service, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def get(path, expected=200):
            connection = http.client.HTTPConnection(*server.server_address, timeout=30)
            begun = time.perf_counter()
            connection.request("GET", path)
            response = connection.getresponse()
            body = response.read()
            elapsed.append(time.perf_counter() - begun)
            connection.close()
            assert response.status == expected, body
            responses.append(
                dict(
                    path=path,
                    status=response.status,
                    sha256=hashlib.sha256(body).hexdigest(),
                    body_utf8=body.decode(),
                )
            )
            return json.loads(body) if path != "/" else body.decode()

        try:
            assert "cursor-ps" in get("/")
            for channel in (0, 1):
                manifest = get(f"/runs/recording-c{channel}/manifest")
                assert manifest["exact_tick_cursor"] and manifest["digital_history_recorded"]
            saved = service.snapshot
            chains = {c: Chain(replace(cfg)) for c, cfg in saved.configs.items()}
            columns = []
            for channel, chain in chains.items():
                first = chain.advance(113).codes
                if channel == 0:
                    chain.cfg.detector = replace(chain.cfg.detector, gain=2400.0)
                columns.append(np.concatenate([first, chain.advance(674).codes]))
                assert chain.checkpoint() == saved.current.chains[channel].checkpoint()
            source = ConvertedSamples(
                np.column_stack(columns),
                saved.current.channels,
                0,
                0,
                saved.current.domain,
                saved.link.bits,
                **saved.current.identity,
            )
            reference = DigitalPipeline(
                saved.current.domain,
                saved.link,
                saved.acquisition,
                saved.stages,
                saved.current.channels,
            )
            reference.schedule_syncs([20_000_000, 50_000_000, 80_000_000])
            for part in split_by_epoch(source, saved.current.controller):
                reference.link.push(part)
            delivered = lost = retained = 0
            ticks = sorted({*range(0, 110_000_001, 500_000), 25_000_001, 84_250_001, 196_749_999})
            for tick in ticks:
                reference.advance_to(tick)
                batch = reference.drain()
                delivered += len(batch.delivered)
                lost += len(batch.losses)
                retained += sum(len(p.packet.source) for p in batch.delivered)
                for channel in (0, 1):
                    actual = get(f"/runs/recording-c{channel}/digital-state?tick={tick}&req={tick}")
                    assert actual["counts"] == dict(
                        delivered_packets=str(delivered),
                        lost_packets=str(lost),
                        retained_rows=str(retained),
                    )
                    assert actual["source"]["sampled_rows"] == str(min(197, tick // 1_000_000 + 1))
                    assert [int(s["resident_bytes"]) for s in actual["transport"]["stages"]] == [
                        s.bytes for s in reference.transport.stages
                    ]
                    assert [int(s["overflow_count"]) for s in actual["transport"]["stages"]] == [
                        s.stats.overflow_count for s in reference.transport.stages
                    ]
                    inflight = sum(
                        np.count_nonzero(
                            (p.output.sample_time <= tick) & (p.output.capture_time > tick)
                        )
                        for p in reference.link.pending
                    )
                    assert int(actual["link"]["in_flight_rows"]) == inflight
                    window = reference.acquisition.active
                    active = actual["acquisition"]["active"]
                    assert (active is None) == (window is None)
                    if window is not None:
                        assert int(active["retained_rows"]) == sum(len(s) for s in window.segments)
                        assert active["epochs"] == sorted({s.epoch for s in window.segments})
            for channel in (0, 1):
                prefix = f"/runs/recording-c{channel}"
                assert (
                    get(prefix + "/digital-state?tick=25000001&view=instrument")["status"]
                    == "not_recorded"
                )
                get(prefix + "/events?start=0&end=787")
                get(prefix + "/state?step=100&probes=power_w")
                get(prefix + "/samples/15/lineage")
            for tick in ("01", "-0", "1.5", "9223372036854775808", "196750000"):
                get("/runs/recording-c0/digital-state?tick=" + tick, 400)
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
    assert hashes(folder) == before
    path = root / "responses.json"
    if verify:
        (root / f"readback_{time.time_ns()}.json").write_text(
            json.dumps(responses, indent=2) + "\n"
        )
        assert responses == json.loads(path.read_text())
    else:
        path.write_text(json.dumps(responses, indent=2) + "\n")
        (root / "timings.json").write_text(
            json.dumps(
                dict(
                    scope="787 fine steps, two channels; HTTP; uncontrolled cache; no drawing",
                    requests=[
                        dict(path=row["path"], elapsed_s=seconds)
                        for row, seconds in zip(responses, elapsed, strict=True)
                    ],
                ),
                indent=2,
            )
            + "\n"
        )
    sizes = [p.stat().st_size for p in (folder / "history").glob("*.json")]
    return dict(
        origin="SIMULATED",
        visibility="ORACLE_ONLY",
        source_files=len(before),
        exact_time_queries=2 * len(ticks),
        http_responses=len(responses),
        chronological_reference_match=True,
        source_unchanged=True,
        archived_checkpoint_bytes=dict(count=len(sizes), minimum=min(sizes), maximum=max(sizes)),
        closure="inclusive; source interval only; no EOF drain projection",
        scope="Same-host CLI/HTTP and software reference; no browser or hardware acceptance",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    root = args.out.resolve()
    if args.verify:
        assert query(root, True) == json.loads((root / "audit.json").read_text())
        print("fresh-process HTTP bytes and chronological reference match")
        return
    root.mkdir(parents=True, exist_ok=False)
    cfg = json.loads(
        (Path(__file__).resolve().parents[1] / "configs/physical_recording.json").read_text()
    )
    cfg.update(chunk_steps=97, record_digital_history=True, record_quality=True)
    cfg["chain"]["source"]["gated"] = False
    cfg["chain"]["detector"].update(shot_noise=True, pole_hz=70000.0)
    cfg["chain"]["psf"].update(nodes_x=1, nodes_y=1)
    cfg["acquisition"] = dict(window_start=2, window_length=13)
    (root / "config.json").write_text(json.dumps(cfg, indent=2) + "\n")
    rows = [
        dict(op="sync", ticks=["20000000", "50000000", "80000000"]),
        dict(op="advance", steps=57),
        dict(op="request", channel=0, parameter="detector.gain", value=2400.0, effective_step=113),
        dict(op="advance", steps=280),
        dict(op="reset"),
        dict(op="start"),
        dict(op="advance", steps=450),
        dict(op="finish"),
    ]
    (root / "commands.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    argv = [
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
    ]
    result = subprocess.run(argv, capture_output=True, text=True)
    (root / "producer.stdout").write_text(result.stdout)
    (root / "producer.stderr").write_text(result.stderr)
    (root / "producer_receipt.json").write_text(
        json.dumps(dict(argv=argv, returncode=result.returncode))
    )
    assert result.returncode == 0, result.stderr
    (root / "source_manifest.json").write_text(json.dumps(hashes(root / "source"), indent=2) + "\n")
    (root / "audit.json").write_text(json.dumps(query(root), indent=2) + "\n")
    print("real HTTP digital cursor and chronological reference passed")


if __name__ == "__main__":
    main()
