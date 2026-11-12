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


def test_cli_preserves_failed_fit_diagnostics_and_reconstruction_refuses_them(tmp_path, capsys):
    from zlscan.cli import CHECK_FAIL_EXIT_CODE, main

    cfg = demo_config()
    cfg.detector = replace(cfg.detector, shot_noise=False)
    scan = tmp_path / "scan"
    run_calibration_scan(scan, cfg, lines_per_target=2)
    observable = scan / "observable"
    protocol_path = observable / "scan_protocol.json"
    protocol = json.loads(protocol_path.read_text())
    protocol["transfer_reference_tolerance_code"] = 0
    protocol_path.write_text(json.dumps(protocol))
    out = tmp_path / "calibration"
    raw = observable / "raw.zlraw"
    code = main(
        ["calibrate", "--raw", str(raw), "--protocol", str(protocol_path), "--out", str(out)]
    )
    assert code == CHECK_FAIL_EXIT_CODE
    calibration = load_calibration(out / "calibration.json")
    assessment = calibration.items["transfer_lut"].value["channels"]["0"]["reference_assessment"]
    assert assessment["status"] == "mismatch"
    assert calibration.generator["version"] == 2
    summary = json.loads((out / "run.json").read_text())
    assert summary["transfer_reference_assessments"]["0"] == assessment
    reconstructed = tmp_path / "reconstruction"
    code = main(
        [
            "reconstruct",
            "--raw",
            str(raw),
            "--capture",
            str(observable / "capture.json"),
            "--calibration",
            str(out / "calibration.json"),
            "--out",
            str(reconstructed),
            "--columns",
            "40",
            "--pitch-x-m",
            ".002",
            "--pitch-y-m",
            ".002",
        ]
    )
    assert code == CHECK_FAIL_EXIT_CODE
    assert "reference" in capsys.readouterr().err
    assert not (reconstructed / "run.json").exists()


def test_monotonic_cli_requires_declared_uncertainty_and_writes_supported_table(tmp_path, capsys):
    from zlscan.cli import CHECK_FAIL_EXIT_CODE, main

    cfg = demo_config()
    cfg.detector = replace(cfg.detector, shot_noise=False)
    scan = tmp_path / "scan"
    run_calibration_scan(scan, cfg, lines_per_target=2)
    observable = scan / "observable"
    protocol_path = observable / "scan_protocol.json"
    raw = observable / "raw.zlraw"
    original_sha = sha256_file(raw)
    common = [
        "calibrate",
        "--raw",
        str(raw),
        "--protocol",
        str(protocol_path),
        "--transfer-method",
        "monotonic",
    ]
    assert main([*common, "--out", str(tmp_path / "missing")]) == CHECK_FAIL_EXIT_CODE
    assert not (tmp_path / "missing/calibration.json").exists()
    protocol = json.loads(protocol_path.read_text())
    values = np.r_[0.0, 1.0, 10 ** -np.asarray(protocol["wedge_density"])]
    protocol["monotonic_uncertainty_source"] = (
        "ASSUMED deterministic reference: +/-2 ADC codes; exact synthetic ratios"
    )
    protocol["monotonic_code_half_width"] = [2.0] * len(values)
    protocol["monotonic_signal_bounds"] = [[float(v), float(v)] for v in values]
    protocol_path.write_text(json.dumps(protocol))
    output = tmp_path / "table"
    assert main([*common, "--out", str(output)]) == 0
    table = load_calibration(output / "calibration.json")
    assert table.format_version == 2
    assert table.generator["name"] == "monotonic_transfer"
    transfer = table.items["transfer_lut"].value["channels"]["0"]
    assert transfer["normalization"]["per_column_correction"] is False
    assert transfer["valid_code_ranges"]
    assert transfer["reference_assessment"]["status"] == "not_assessed"
    reconstruction = tmp_path / "reconstructed"
    assert (
        main(
            [
                "reconstruct",
                "--raw",
                str(raw),
                "--capture",
                str(observable / "capture.json"),
                "--calibration",
                str(output / "calibration.json"),
                "--out",
                str(reconstruction),
                "--columns",
                "40",
                "--pitch-x-m",
                ".002",
                "--pitch-y-m",
                ".002",
            ]
        )
        == 0
    )
    assert json.loads((reconstruction / "run.json").read_text())["valid_fraction"] > 0
    assert sha256_file(raw) == original_sha
    assert "uncertainty" in capsys.readouterr().err
