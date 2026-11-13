"""Synthetic observed references -> actual calibrate/set/reconstruct/report CLI and readback.

The code response and holdout density are declared test assumptions. No real measurements or
parameter upgrades are claimed. The calibration implementation receives only raw references
and the operator's known-target protocol, never the holdout density or generator parameters.
"""

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

from zlscan.api.server import ImageRun, Service, make_server
from zlscan.calib.file import load_calibration
from zlscan.calib.observations import calibration_from_scan
from zlscan.contracts.calibration import Binding, CaptureDescription, EpochBinding
from zlscan.io.image_artifact import ImageArtifact
from zlscan.io.manifest import sha256_file
from zlscan.io.rawstream import (
    FLAG_EPOCH_START,
    FLAG_LINE_END,
    FLAG_LINE_START,
    RawWriter,
    SessionHeader,
)


def session(identity):
    return SessionHeader(
        schema_version="0.1",
        session_id=identity,
        created_at="2026-11-13T00:00:00Z",
        hardware_hash=None,
        firmware_hash=None,
        wiring_rev=None,
        register_dump_hash=None,
        calibration_id=None,
        channel_roles={"0": "signal"},
        clock_domains=[dict(id=1, freq_num=1000000, freq_den=1, phase_ticks=0)],
        coding="offset_binary",
        bits_per_sample=16,
        bytes_per_sample=2,
        windows=dict(window_length=24),
        origin="SIMULATED",
    )


def codes(transmittance, epoch):
    return np.rint(
        20000 + (4000 + 2000 * epoch) * np.log10(0.01 + np.asarray(transmittance))
    ).astype(np.uint16)


def sources(root):
    bindings = []
    densities = [0, 0.2, 0.4, 0.8, 1.2, 1.6]
    for epoch in (0, 1):
        folder = root / f"point_{epoch}"
        folder.mkdir()
        binding = Binding(
            simulation_hash=f"analytic-log-point-{epoch}",
            channel_roles={"0": "signal"},
            gain_settings={"pmt": "low_assumed" if epoch == 0 else "high_assumed"},
            epoch_range=(epoch, epoch),
        )
        bindings.append(binding)
        raw = folder / "reference.zlraw"
        with RawWriter(raw, session(201 + epoch)) as writer:
            for line in range(6):
                transmittance = (
                    np.zeros(24)
                    if line < 2
                    else np.ones(24)
                    if line < 4
                    else np.repeat(10.0 ** -np.asarray(densities), 4)
                )
                writer.write_block(
                    epoch=epoch,
                    line_number=line,
                    channel_mask=1,
                    clock_domain=1,
                    first_sample_index=100 + line * 100,
                    samples=codes(transmittance, epoch)[:, None],
                    flags=FLAG_LINE_START | FLAG_LINE_END | (FLAG_EPOCH_START if line == 0 else 0),
                )
        protocol = dict(
            version=1,
            origin="SIMULATED",
            binding=binding.model_dump(mode="json"),
            raw_sha256=sha256_file(raw),
            width_m=24,
            pitch_y_m=1,
            targets=[
                dict(name=name, first_line=i * 2, lines=2)
                for i, name in enumerate(("dark", "white", "wedge"))
            ],
            wedge_density=densities,
            edge_fraction=0.5,
            grid_line_x_fraction=[0.25, 0.75],
            slanted_line_x_fractions=[0.2, 0.8],
        )
        (folder / "protocol.json").write_text(
            json.dumps(protocol, indent=2) + "\n", encoding="utf-8"
        )
    raw = root / "sample.zlraw"
    with RawWriter(raw, session(301)) as writer:
        for epoch in (0, 1):
            writer.write_block(
                epoch=epoch,
                line_number=0,
                channel_mask=1,
                clock_domain=1,
                first_sample_index=100 + epoch * 12,
                samples=np.full((12, 1), codes(10**-0.6, epoch), np.uint16),
                flags=(FLAG_LINE_START if epoch == 0 else FLAG_LINE_END) | FLAG_EPOCH_START,
            )
    capture = CaptureDescription(
        version=2,
        raw_sha256=sha256_file(raw),
        lines=1,
        binding=Binding(channel_roles={"0": "signal"}, epoch_range=(0, 1)),
        epoch_bindings=[EpochBinding(epoch=i, binding=b) for i, b in enumerate(bindings)],
    )
    (root / "capture.json").write_text(capture.model_dump_json(indent=2) + "\n", encoding="utf-8")
    (root / "assumptions.json").write_text(
        json.dumps(
            dict(
                origin="SYNTHETIC",
                holdout_density_od=0.6,
                code_response="round(20000+(4000+2000*epoch)*log10(0.01+T))",
                criterion_max_abs_density_error_od=0.002,
                actual_measurements_obtained=False,
                parameter_upgrades=[],
            ),
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def source_manifest(root):
    paths = [
        root / "sample.zlraw",
        root / "capture.json",
        root / "assumptions.json",
        root / "calibrations.json",
    ]
    for epoch in (0, 1):
        paths += [
            root / f"point_{epoch}" / name
            for name in ("reference.zlraw", "protocol.json", "fit/calibration.json")
        ]
    return {p.relative_to(root).as_posix(): sha256_file(p) for p in paths}


def verify(root):
    expected = json.loads((root / "assumptions.json").read_text(encoding="utf-8"))
    assert source_manifest(root) == json.loads(
        (root / "source_manifest.json").read_text(encoding="utf-8")
    )
    calibration = load_calibration(root / "calibrations.json")
    for epoch in (0, 1):
        folder = root / f"point_{epoch}"
        recomputed = calibration_from_scan(
            folder / "reference.zlraw", folder / "protocol.json", fit_transfer=True
        )
        saved = load_calibration(folder / "fit/calibration.json")
        assert {k: v.model_dump(mode="json") for k, v in recomputed.items.items()} == {
            k: v.model_dump(mode="json") for k, v in saved.items.items()
        }
        assert recomputed.binds_to == saved.binds_to
        assert recomputed.calibration_id == saved.calibration_id
        assert saved in calibration.calibrations
    run = json.loads((root / "image/run.json").read_text(encoding="utf-8"))
    assert run["epoch_calibrations"] == {
        str(i): c.calibration_id for i, c in enumerate(calibration.calibrations)
    }
    with np.load(root / "image/density.npz") as arrays:
        assert not arrays["flags"].any()
        error = float(np.max(np.abs(arrays["density"] - expected["holdout_density_od"])))
        assert error < expected["criterion_max_abs_density_error_od"]
    assert not (root / "rejected").exists()
    responses = []
    with ImageArtifact(root / "image", raw_path=root / "sample.zlraw") as artifact:
        service = Service()
        service.add(ImageRun("epochs", artifact))
        server = make_server(service, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for column in range(24):
                connection = http.client.HTTPConnection(*server.server_address, timeout=30)
                connection.request("GET", f"/runs/epochs/images/density/pixels/0/{column}/lineage")
                response = connection.getresponse()
                body = response.read()
                connection.close()
                assert response.status == 200, body
                result = json.loads(body)
                assert (
                    result["samples"][0]["calibration_id"]
                    == calibration.calibrations[column // 12].calibration_id
                )
                assert result["samples"][0]["sample_index"] == str(100 + column)
                responses.append(
                    dict(
                        column=column,
                        body=json.loads(body),
                        sha256=hashlib.sha256(body).hexdigest(),
                    )
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=10)
        assert not thread.is_alive()
    return dict(
        origin="SYNTHETIC",
        actual_measurements_obtained=False,
        parameter_upgrades=[],
        max_abs_density_error_od=error,
        criterion_od=expected["criterion_max_abs_density_error_od"],
        epoch_calibrations=run["epoch_calibrations"],
        http_responses=responses,
        source_files_unchanged=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    root = args.out.resolve()
    if args.verify:
        assert verify(root) == json.loads((root / "audit.json").read_text(encoding="utf-8"))
        print("fresh-process epoch calibration verification passed")
        return
    root.mkdir(parents=True, exist_ok=False)
    sources(root)
    commands = []

    def run(label, arguments, expected=0):
        argv = [sys.executable, "-I", "-X", "utf8", "-m", "zlscan", *map(str, arguments)]
        result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8")
        (root / (label + ".stdout")).write_text(result.stdout, encoding="utf-8")
        (root / (label + ".stderr")).write_text(result.stderr, encoding="utf-8")
        commands.append(
            dict(label=label, argv=argv, returncode=result.returncode, expected=expected)
        )
        (root / "commands.json").write_text(json.dumps(commands, indent=2) + "\n", encoding="utf-8")
        assert result.returncode == expected, (label, result.stdout, result.stderr)

    calibrations = []
    for epoch in (0, 1):
        folder = root / f"point_{epoch}"
        run(
            f"calibrate_{epoch}",
            [
                "calibrate",
                "--raw",
                folder / "reference.zlraw",
                "--protocol",
                folder / "protocol.json",
                "--out",
                folder / "fit",
            ],
        )
        calibrations.append(load_calibration(folder / "fit/calibration.json"))
    run(
        "calibration_set",
        [
            "calibration-set",
            "--id",
            "synthetic-two-observed-working-points",
            "--calibration",
            root / "point_0/fit/calibration.json",
            "--calibration",
            root / "point_1/fit/calibration.json",
            "--out",
            root / "calibrations.json",
        ],
    )
    reconstruct = [
        "reconstruct",
        "--raw",
        root / "sample.zlraw",
        "--capture",
        root / "capture.json",
        "--columns",
        "24",
        "--pitch-x-m",
        "1",
        "--pitch-y-m",
        "1",
    ]
    run(
        "reconstruct",
        reconstruct + ["--calibration", root / "calibrations.json", "--out", root / "image"],
    )
    run(
        "reject_common_header",
        reconstruct
        + ["--calibration", root / "point_0/fit/calibration.json", "--out", root / "rejected"],
        1,
    )
    run(
        "report",
        [
            "report",
            "--run",
            root / "image",
            "--raw",
            root / "sample.zlraw",
            "--out",
            root / "image.html",
        ],
    )
    (root / "source_manifest.json").write_text(
        json.dumps(source_manifest(root), indent=2) + "\n", encoding="utf-8"
    )
    result = verify(root)
    (root / "audit.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"origin": "SYNTHETIC", "max_abs_density_error_od": result["max_abs_density_error_od"]}
        )
    )


if __name__ == "__main__":
    main()
