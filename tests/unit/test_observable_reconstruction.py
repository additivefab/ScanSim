import json
import re
import shutil
from dataclasses import replace

import numpy as np
import pytest

from zlscan.cli import main
from zlscan.config.presets import demo_config
from zlscan.engine.physical_run import run_physical
from zlscan.experiment.calibration_scan import run_calibration_scan
from zlscan.truth.scene import uniform


def test_cli_reconstruction_is_isolated_and_sensitive_to_observed_light(tmp_path):
    cfg = demo_config()
    cfg.detector = replace(cfg.detector, shot_noise=False)
    reference = tmp_path / "reference"
    run_calibration_scan(reference, cfg, lines_per_target=4)
    reference_obs = reference / "observable"
    cal = tmp_path / "calibration"
    assert (
        main(
            [
                "calibrate",
                "--raw",
                str(reference_obs / "raw.zlraw"),
                "--protocol",
                str(reference_obs / "scan_protocol.json"),
                "--out",
                str(cal),
            ]
        )
        == 0
    )

    def reconstruct(observable, output, calibration_dir=cal):
        assert (
            main(
                [
                    "reconstruct",
                    "--raw",
                    str(observable / "raw.zlraw"),
                    "--capture",
                    str(observable / "capture.json"),
                    "--calibration",
                    str(calibration_dir / "calibration.json"),
                    "--out",
                    str(output),
                    "--columns",
                    "40",
                    "--pitch-x-m",
                    "0.002",
                    "--pitch-y-m",
                    "0.002",
                ]
            )
            == 0
        )
        with np.load(output / "density.npz") as result:
            return result["density"]

    def scan(density, name):
        instrument = replace(cfg, scenes={"main": uniform(density, 0.08, 0.2)})
        out = tmp_path / name
        run_physical(out, instrument, 4)
        isolated = tmp_path / f"{name}-isolated"
        shutil.copytree(out / "observable", isolated)
        return out, isolated

    source, bright = scan(0.7, "bright")
    baseline = reconstruct(bright, tmp_path / "result")
    np.save(source / "truth/source_codes.npy", np.zeros(4, dtype=np.uint16))
    repeated = reconstruct(bright, tmp_path / "repeated")
    np.testing.assert_array_equal(repeated, baseline)
    _, dark = scan(1.0, "dark")
    dimmed = reconstruct(dark, tmp_path / "dimmed")
    assert np.nanmedian(dimmed - baseline) == pytest.approx(0.3, abs=0.015)

    report = tmp_path / "image.html"
    assert (
        main(
            [
                "report",
                "--run",
                str(tmp_path / "result"),
                "--raw",
                str(bright / "raw.zlraw"),
                "--compare",
                str(tmp_path / "dimmed"),
                "--compare-raw",
                str(dark / "raw.zlraw"),
                "--out",
                str(report),
            ]
        )
        == 0
    )
    html = report.read_text()
    payload = re.search(r'<script id="image-data" type="application/json">(.*?)</script>', html)
    assert payload is not None
    images = json.loads(payload.group(1))["images"]
    assert len(images) == 2
    assert images[0]["lineage"]["lines"]["1"]["clock"]["freq_num"] == 1_000_000
    assert "http://" not in html and "https://" not in html

    protocol = json.loads((reference_obs / "scan_protocol.json").read_text())
    edge_tick = 4_000_000
    ticks = np.arange(0, 16_000_000, 100_000, dtype=np.int64)
    timing = tmp_path / "timing.json"
    timing.write_text(
        json.dumps(
            {
                "raw_sha256": protocol["raw_sha256"],
                "ticks": ticks.tolist(),
                "values": (
                    -np.expm1(-np.maximum(ticks - edge_tick - 1_000_000, 0) / 500_000)
                ).tolist(),
                "monitor": {
                    "kind": "injection_monitor",
                    "origin": "SIMULATED",
                    "visibility": "OBSERVABLE",
                    "derivation": "SENSOR",
                    "clock_domain": 1,
                    "count": 4,
                    "tick": edge_tick,
                    "session_id": 1,
                    "epoch": 0,
                    "capability_ref": "fixture/injection_monitor",
                },
            }
        )
    )
    timed_cal = tmp_path / "timed-calibration"
    assert (
        main(
            [
                "calibrate",
                "--raw",
                str(reference_obs / "raw.zlraw"),
                "--protocol",
                str(reference_obs / "scan_protocol.json"),
                "--timing",
                str(timing),
                "--out",
                str(timed_cal),
            ]
        )
        == 0
    )
    timed = tmp_path / "timed-reconstruction"
    reconstruct(bright, timed, timed_cal)
    assert json.loads((timed / "run.json").read_text())["delay_offset_samples"] == 1.0
