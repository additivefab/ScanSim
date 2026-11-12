import json
from dataclasses import replace

import pytest

from zlscan.calib.observations import calibration_from_scan
from zlscan.config.presets import demo_config
from zlscan.experiment.calibration_scan import run_calibration_scan
from zlscan.io.rawstream import read_all


def test_facet_fit_recovers_physical_grid_injections_from_observed_adc_codes(tmp_path):
    cfg = demo_config()
    cfg.detector = replace(cfg.detector, shot_noise=False)
    cfg.scan = replace(
        cfg.scan,
        distortion_m=0.0012,
        facet_offset_m=(-0.00045, 0.00045, -0.00045, 0.00045),
    )
    out = tmp_path / "scan"
    run_calibration_scan(out, cfg)
    observable = out / "observable"
    raw_path = observable / "raw.zlraw"
    protocol_path = observable / "scan_protocol.json"
    protocol = json.loads(protocol_path.read_text())
    raw = read_all(raw_path)
    window = raw.session.windows
    observations = []
    for block in raw.blocks:
        index = block.first_sample_index - window["window_start"] - window["sync_latency_cycles"]
        sync = {
            "kind": "line_sync",
            "origin": "SIMULATED",
            "visibility": "OBSERVABLE",
            "derivation": "SENSOR",
            "capability_ref": "fixture/sos",
            "clock_domain": 1,
            "count": index,
            "tick": index * 1_000_000,
            "session_id": 1,
            "epoch": 0,
        }
        line = {"line": block.line_number, "sync": sync}
        if not observations:
            line["anchor"] = {
                **sync,
                "kind": "facet_index",
                "capability_ref": "fixture/index",
                "payload": {"phase": 0},
            }
        observations.append(line)
    facets = tmp_path / "timing.json"
    facets.write_text(
        json.dumps(
            {
                "channel": 0,
                "timing": {
                    "raw_sha256": protocol["raw_sha256"],
                    "policy": {
                        "facets": 4,
                        "period_ticks": 2_500_000_000,
                        "tolerance_ticks": 0,
                        "phase_reference": "fixture/index",
                    },
                    "lines": observations,
                },
            }
        )
    )
    result = calibration_from_scan(raw_path, protocol_path, facet_path=facets)
    mapping = result.items["facet_geometry"].value["facets"]
    assert set(mapping) == {"0", "1", "2", "3"}
    for facet in mapping.values():
        assert facet["x_coefficients_m"][2] == pytest.approx(0.0012, abs=0.0002)
    delta = mapping["1"]["x_coefficients_m"][0] - mapping["0"]["x_coefficients_m"][0]
    # Error budget permits roughly one acquired sample at the demo's 1 MS/s scan rate.
    assert delta == pytest.approx(0.0009, abs=0.00016)
