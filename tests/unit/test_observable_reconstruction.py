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

    def reconstruct(observable, output):
        assert (
            main(
                [
                    "reconstruct",
                    "--raw",
                    str(observable / "raw.zlraw"),
                    "--capture",
                    str(observable / "capture.json"),
                    "--calibration",
                    str(cal / "calibration.json"),
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
