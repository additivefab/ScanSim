import json
from dataclasses import replace

import numpy as np
import pytest

from zlscan.calib.file import load_calibration, require_applicable, save_calibration
from zlscan.calib.observations import calibration_from_scan
from zlscan.config.presets import demo_config
from zlscan.contracts.calibration import Binding, Provenance
from zlscan.experiment.calibration_scan import run_calibration_scan
from zlscan.io.manifest import sha256_file
from zlscan.io.rawstream import read_all


def test_calibration_protocol_is_backed_by_a_continuous_observable_capture(tmp_path):
    config = demo_config()
    config.detector = replace(config.detector, shot_noise=False)
    result = run_calibration_scan(tmp_path, config, lines_per_target=4)
    observable = tmp_path / "observable"
    protocol = json.loads((observable / "scan_protocol.json").read_text())
    raw = observable / "raw.zlraw"
    capture = read_all(raw)
    assert result["integrity"] == "complete"
    assert protocol["raw_sha256"] == sha256_file(raw)
    assert [phase["name"] for phase in protocol["targets"]] == [
        "dark",
        "white",
        "wedge",
        "edge",
        "grid",
        "slanted_line",
    ]
    assert [phase["first_line"] for phase in protocol["targets"]] == list(range(0, 24, 4))
    assert [block.line_number for block in capture.blocks] == list(range(24))
    assert {block.epoch for block in capture.blocks} == {0}
    # The demo's negative polarity maps more light to lower raw codes.
    assert np.median(capture.segments[0].samples) > np.median(capture.segments[5].samples)

    protocol_path = observable / "scan_protocol.json"
    calibration = calibration_from_scan(raw, protocol_path)
    assert calibration.provenance is Provenance.SIMULATED
    assert calibration.generator["raw_sha256"] == protocol["raw_sha256"]
    require_applicable(calibration, Binding.model_validate(protocol["binding"]))
    path = observable / "calibration.json"
    save_calibration(path, calibration)
    assert load_calibration(path) == calibration
    dark_rows = np.stack([segment.samples[:, 0] for segment in capture.segments[:4]])
    np.testing.assert_array_equal(
        calibration.items["dark_raw_code"].value["0"]["median"],
        np.median(dark_rows, axis=0),
    )
    fitted = calibration_from_scan(raw, protocol_path, fit_transfer=True)
    assert fitted.calibration_id != calibration.calibration_id
    assert fitted.items["transfer_lut"].value["extrapolation"] == "invalid"
    assert fitted.items["transfer_lut"].value["channels"]["0"]["slope_codes_per_decade"] < 0

    protocol["raw_sha256"] = "different"
    protocol_path.write_text(json.dumps(protocol), encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256"):
        calibration_from_scan(raw, protocol_path)
