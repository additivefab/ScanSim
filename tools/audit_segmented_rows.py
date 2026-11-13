"""Synthetic observable references, real digital epoch packetization and public reconstruction."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np

from zlscan.adc.patterns import ConvertedSamples
from zlscan.adc_link.link import LinkConfig
from zlscan.api.server import ImageRun, Service, make_server
from zlscan.calib.file import load_calibration, save_calibration
from zlscan.contracts.calibration import Binding, CaptureDescription
from zlscan.contracts.time import ClockDomain
from zlscan.digital.acquisition import AcquisitionConfig
from zlscan.engine.digital_pipeline import DigitalPipeline
from zlscan.engine.digital_recording import DigitalRecorder
from zlscan.io.image_artifact import ImageArtifact
from zlscan.io.manifest import sha256_file
from zlscan.io.rawstream import (
    FLAG_EPOCH_START,
    FLAG_LINE_END,
    FLAG_LINE_START,
    RawWriter,
    SessionHeader,
    read_all,
)
from zlscan.transport.queues import StageSpec


def codes(transmittance):
    return np.rint(40000 + 8000 * np.log10(np.asarray(transmittance) + 0.001)).astype(np.uint16)


def binding(epoch_range):
    return Binding(
        simulation_hash="synthetic-static-unchanged-binding",
        channel_roles={"0": None},
        gain_settings={"0": "unchanged_synthetic"},
        epoch_range=epoch_range,
    )


def references(root, run):
    density = np.arange(6) * 0.5
    calibrations = []
    for epoch in (0, 1):
        folder = root / f"reference-{epoch}"
        folder.mkdir()
        session = SessionHeader(
            schema_version="0.1",
            session_id=19,
            created_at="2026-11-13T00:00:00Z",
            hardware_hash=None,
            wiring_rev=None,
            firmware_hash=None,
            register_dump_hash=None,
            calibration_id=None,
            channel_roles={"0": None},
            clock_domains=[dict(id=1, freq_num=1_000_000)],
            coding="offset_binary",
            bits_per_sample=16,
            bytes_per_sample=2,
            windows=dict(window_length=24),
            origin="SIMULATED",
        )
        raw = folder / "raw.zlraw"
        with RawWriter(raw, session) as writer:
            for line in range(6):
                values = codes(
                    np.zeros(24)
                    if line < 2
                    else np.ones(24)
                    if line < 4
                    else np.repeat(10**-density, 4)
                )
                writer.write_block(
                    epoch=epoch,
                    channel_mask=1,
                    clock_domain=1,
                    first_sample_index=1000 + line * 30,
                    samples=values[:, None],
                    line_number=line,
                    flags=FLAG_LINE_START | FLAG_LINE_END | (FLAG_EPOCH_START if line == 0 else 0),
                )
        protocol = dict(
            version=1,
            origin="SIMULATED",
            binding=binding((epoch, epoch)).model_dump(mode="json"),
            raw_sha256=sha256_file(raw),
            width_m=0.024,
            pitch_y_m=0.001,
            targets=[
                dict(name=name, first_line=i * 2, lines=2)
                for i, name in enumerate(("dark", "white", "wedge"))
            ],
            wedge_density=density.tolist(),
            transfer_reference_tolerance_code=1.0,
            edge_fraction=0.5,
            grid_line_x_fraction=[],
            slanted_line_x_fractions=[0.2, 0.8],
        )
        (folder / "protocol.json").write_text(
            json.dumps(protocol, indent=2) + "\n", encoding="utf-8"
        )
        run(
            f"calibrate-{epoch}",
            [
                "calibrate",
                "--raw",
                str(raw),
                "--protocol",
                str(folder / "protocol.json"),
                "--out",
                str(folder / "calibration"),
            ],
        )
        calibrations.append(load_calibration(folder / "calibration/calibration.json"))
    # This joint fixture has observed references in BOTH epochs, with identical fitted items.
    # It is not a general widening operation or a per-epoch/multiple-working-point selector.
    assert calibrations[0].items == calibrations[1].items
    ids = [c.calibration_id for c in calibrations]
    joint = calibrations[0].model_copy(deep=True)
    joint.binds_to = binding((0, 1))
    joint.calibration_id = hashlib.sha256(json.dumps(ids).encode()).hexdigest()
    joint.generator = dict(
        name="synthetic-identical-two-epoch-references",
        calibration_ids=ids,
        reference_file_sha256=[
            sha256_file(root / f"reference-{e}/calibration/calibration.json") for e in (0, 1)
        ],
    )
    joint.valid_conditions["scope"] = (
        "synthetic static fixture; exactly equal epoch reference items"
    )
    save_calibration(root / "common_calibration.json", joint)


def verify(root, readback=False):
    expected = np.repeat(np.array([0.25, 0.65, 1.05, 1.45, 1.85, 2.25]), 4)
    with np.load(root / "split/density.npz") as image:
        assert not image["flags"].any()
        error = float(np.max(np.abs(image["density"][0] - expected)))
        assert error < 0.002
    source = root / "producer/raw.zlraw"
    raw = read_all(source)
    assert [(b.epoch, b.sample_count) for b in raw.blocks] == [(0, 12), (1, 12)]
    assert not raw.blocks[1].flags & FLAG_LINE_START
    responses = []
    with ImageArtifact(root / "split", raw_path=source) as artifact:
        service = Service()
        service.add(ImageRun("split", artifact))
        server = make_server(service, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            paths = [
                "/runs/split/manifest",
                "/runs/split/images/density/tiles/0/0/0",
                "/runs/split/traces?signal=adc.code&start=0&end=24&pixel_width=1",
                "/runs/split/state?step=20",
            ]
            paths += [f"/runs/split/images/density/pixels/0/{col}/lineage" for col in range(24)]
            for path in paths:
                connection = http.client.HTTPConnection(*server.server_address, timeout=30)
                connection.request("GET", path)
                response = connection.getresponse()
                body = response.read()
                connection.close()
                assert response.status == 200, body
                responses.append(
                    dict(
                        path=path, body_utf8=body.decode(), sha256=hashlib.sha256(body).hexdigest()
                    )
                )
            trace = json.loads(responses[2]["body_utf8"])
            assert trace["mixed_epoch"] == [True] and trace["epochs"] == [[0, 1]]
            assert json.loads(responses[3]["body_utf8"])["epoch"] == 1
            sample = json.loads(responses[-1]["body_utf8"])["samples"][0]
            assert sample["epoch"] == 1 and sample["sample_index"] == "123"
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
    with ImageArtifact(root / "gap", raw_path=root / "gap.zlraw") as artifact:
        assert artifact.traces(0, 24, 40)["missing_count"] == 2
        lineage = artifact.pixel_lineage(0, 12)
        assert lineage["flags"] and lineage["missing_lines"]
        assert all(s["sample_index"] not in ("112", "113") for s in lineage["samples"])
        assert artifact.tile(0, 0)["density"][0][12:14] == [None, None]
    with ImageArtifact(root / "orphan", raw_path=root / "orphan.zlraw") as artifact:
        assert artifact.manifest()["unplaced_blocks"] == 1
        assert artifact.tile(0, 0)["density"][0] == [None] * 24
    path = root / "responses.json"
    if readback:
        (root / "readback_responses.json").write_text(
            json.dumps(responses, indent=2) + "\n", encoding="utf-8"
        )
        assert responses == json.loads(path.read_text(encoding="utf-8"))
    else:
        path.write_text(json.dumps(responses, indent=2) + "\n", encoding="utf-8")
    return dict(
        origin="SIMULATED",
        fixture="analytic static unchanged binding, not physical model validation",
        http_responses=len(responses),
        max_density_error_od=error,
        criterion_od=0.002,
        split_blocks=2,
        source_epochs=[0, 1],
        gap_positions=2,
        unplaced_blocks=1,
    )


def frozen_sources(root):
    excluded = {"audit.json", "responses.json", "readback_responses.json", "source_manifest.json"}
    return {
        str(p.relative_to(root)): sha256_file(p)
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.name not in excluded
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    root = args.out.resolve()
    if args.verify:
        expected_sources = json.loads((root / "source_manifest.json").read_text(encoding="utf-8"))
        assert frozen_sources(root) == expected_sources
        assert verify(root, True) == json.loads((root / "audit.json").read_text(encoding="utf-8"))
        assert frozen_sources(root) == expected_sources
        print("fresh-process image, source epochs and HTTP bytes match")
        return
    root.mkdir(parents=True, exist_ok=False)
    receipts = []

    def run(name, arguments, expected=0):
        argv = [sys.executable, "-m", "zlscan", *arguments]
        result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8")
        (root / (name + ".stdout")).write_text(result.stdout, encoding="utf-8")
        (root / (name + ".stderr")).write_text(result.stderr, encoding="utf-8")
        receipts.append(dict(argv=argv, returncode=result.returncode, expected_returncode=expected))
        (root / "commands.json").write_text(json.dumps(receipts, indent=2) + "\n", encoding="utf-8")
        assert result.returncode == expected, result.stderr

    references(root, run)
    held = np.repeat(np.array([0.25, 0.65, 1.05, 1.45, 1.85, 2.25]), 4)
    words = codes(10**-held)
    clock = ClockDomain(1, 1_000_000)
    pipeline = DigitalPipeline(
        clock,
        LinkConfig(latency_half_cycles=0, propagation_ps=0),
        AcquisitionConfig(0, 24, sync_latency_cycles=0),
        [StageSpec("disk", 4096, 1e12)],
        (0,),
    )
    with DigitalRecorder(root / "producer", pipeline, 19) as recorder:
        recorder.schedule_syncs([clock.tick_of(100)])
        for epoch, start, end in ((0, 0, 12), (1, 12, 24)):
            recorder.push(
                ConvertedSamples(
                    words[start:end, None], (0,), 100 + start, epoch, clock, 16, session_id=19
                )
            )
        recorder.finish()
        recorder.save_checkpoint()
    original = read_all(root / "producer/raw.zlraw")
    for name in ("gap", "orphan"):
        with RawWriter(root / (name + ".zlraw"), original.session) as writer:
            for i, b in enumerate(original.blocks):
                if name == "orphan" and i == 0:
                    continue
                start = 2 if name == "gap" and i == 1 else 0
                data = words[i * 12 + start : (i + 1) * 12, None]
                writer.write_block(
                    epoch=b.epoch,
                    channel_mask=1,
                    clock_domain=1,
                    first_sample_index=b.first_sample_index + start,
                    samples=data,
                    flags=b.flags,
                )
    for name in ("split", "gap", "orphan"):
        raw = root / "producer/raw.zlraw" if name == "split" else root / (name + ".zlraw")
        capture = CaptureDescription(raw_sha256=sha256_file(raw), binding=binding((0, 1)), lines=1)
        description = root / (name + "_capture.json")
        description.write_text(capture.model_dump_json(indent=2) + "\n", encoding="utf-8")
        run(
            name,
            [
                "reconstruct",
                "--raw",
                str(raw),
                "--capture",
                str(description),
                "--calibration",
                str(root / "common_calibration.json"),
                "--out",
                str(root / name),
                "--columns",
                "24",
                "--pitch-x-m",
                ".001",
                "--pitch-y-m",
                ".001",
            ],
            expected=1 if name == "orphan" else 0,
        )
    args_cli = [
        "reconstruct",
        "--raw",
        str(root / "producer/raw.zlraw"),
        "--capture",
        str(root / "split_capture.json"),
        "--calibration",
        str(root / "reference-0/calibration/calibration.json"),
        "--out",
        str(root / "bad_epoch"),
        "--columns",
        "24",
        "--pitch-x-m",
        ".001",
        "--pitch-y-m",
        ".001",
    ]
    run("reject-narrow-epoch", args_cli, 1)
    assert not (root / "bad_epoch").exists()
    run(
        "offline-report",
        [
            "report",
            "--run",
            str(root / "split"),
            "--raw",
            str(root / "producer/raw.zlraw"),
            "--out",
            str(root / "image.html"),
        ],
    )
    (root / "audit.json").write_text(json.dumps(verify(root), indent=2) + "\n", encoding="utf-8")
    (root / "source_manifest.json").write_text(
        json.dumps(frozen_sources(root), indent=2) + "\n", encoding="utf-8"
    )
    print("observable calibration, epoch-split reconstruction and pixel provenance passed")


if __name__ == "__main__":
    main()
