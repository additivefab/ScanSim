"""Component frames come from actual replay, with an earlier ADC hold and no truth in instrument."""

from dataclasses import replace

import numpy as np
from tests.unit.test_digital_state import source
from tests.unit.test_recording_workbench import hashes

from zlscan.config.presets import legacy_dc_config
from zlscan.engine.chain import Chain
from zlscan.engine.saved_service import saved_service


def test_component_probe_observation_preserves_codes_rng_and_state():
    from zlscan.telemetry.components import COMPONENT_PROBES

    cfg = legacy_dc_config(1.0)
    cfg.detector = replace(cfg.detector, shot_noise=True, pole_hz=70000)
    plain, observed = Chain(cfg), Chain(cfg)
    observed.probe_names = COMPONENT_PROBES
    a, b = plain.advance(137), observed.advance(137)
    assert np.array_equal(a.codes, b.codes) and plain.checkpoint() == observed.checkpoint()
    assert b.probes["y_m"].shape == (137,)
    assert b.probes["facet"].dtype.kind in "iu"
    assert b.probes["scan_active"].dtype == bool
    assert b.probes["laser_gate"].dtype == bool


def test_components_match_controlled_replay_and_hold_only_an_earlier_adc_edge(
    tmp_path, monkeypatch
):
    from zlscan.telemetry.components import COMPONENT_PROBES

    folder = source(tmp_path, monkeypatch)
    before = hashes(folder)
    with saved_service(folder) as service:
        for channel in (0, 1):
            run = service.runs[f"recording-c{channel}"]
            frame = service.components(run, 115)
            model = service.snapshot.replay(112, 4, COMPONENT_PROBES)
            assert frame["step"] == "115" and frame["tick_ps"] == "28750000"
            assert frame["visibility"] == "ORACLE_ONLY" and frame["epoch"] == 1
            nodes = {n["id"]: n for n in frame["nodes"]}
            assert nodes["pmt"]["value"] == float(model["current_a"][-1, channel])
            assert nodes["laser"]["value"] == float(model["laser_power_w"][-1, channel])
            assert nodes["adc"]["value"] == int(model["codes"][0, channel])
            assert nodes["adc"]["sample_tick_ps"] == "28000000"
            assert nodes["adc"]["sample_index"] == "28" and nodes["adc"]["held"]
            assert nodes["adc"]["epoch"] == 0  # Held source predates the gain's effective epoch.
            geometry = frame["geometry"]
            assert geometry["x_m"] == model["x_m"][-1, channel]
            assert geometry["y_m"] == model["y_m"][-1, channel]
            assert geometry["x_m"] < 0  # Pre-window trajectory must not be clipped onto the film.
            assert frame["unmodelled_ports"]["log_board_output"] is None
        assert service.components(run, 115, "instrument")["status"] == "not_recorded"
    assert hashes(folder) == before


def test_dark_phase_transmittance_is_not_presented_as_a_physical_one():
    from zlscan.engine.chain import Phase
    from zlscan.telemetry.components import COMPONENT_PROBES, component_frame

    cfg = legacy_dc_config(1.0)
    cfg.timeline = [Phase("dark", 0, 2, next(iter(cfg.scenes)), laser_on=False)]
    chain = Chain(cfg)
    chain.probe_names = COMPONENT_PROBES
    out = chain.advance(4)
    values = {key: value[-1].item() for key, value in out.probes.items()}
    frame = component_frame(cfg, 3, values, int(out.codes[0]), 0, 0)
    film = next(n for n in frame["nodes"] if n["id"] == "film")
    assert film["status"] == "not_computed" and film["value"] is None
    assert frame["geometry"]["laser_gate"] is False
