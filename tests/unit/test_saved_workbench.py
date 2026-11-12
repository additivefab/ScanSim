"""Saved physical queries load artifacts, not a second fabricated demo run."""

import json
from dataclasses import replace

import numpy as np
import pytest

from zlscan.config.presets import demo_config
from zlscan.engine.chain import Chain
from zlscan.engine.physical_run import run_physical
from zlscan.engine.saved_service import saved_service
from zlscan.io.manifest import sha256_file
from zlscan.io.rawstream import read_all


def hashes(path):
    return {str(p.relative_to(path)): sha256_file(p) for p in path.rglob("*") if p.is_file()}


def test_custom_saved_configuration_is_loaded_without_running_chain(tmp_path, monkeypatch):
    cfg = demo_config(seed=19)
    cfg.source = replace(cfg.source, power_w=0.0007)
    source = tmp_path / "source"
    run_physical(source, cfg, 2, save_checkpoints=True)
    before = hashes(source)

    def forbidden(*args, **kwargs):
        raise AssertionError("loader cannot regenerate a simulation")

    monkeypatch.setattr(Chain, "advance", forbidden)
    with saved_service(source) as service:
        run = service.runs["saved"]
        assert run.cfg.hash() == cfg.hash()
        assert service.manifest(run)["loaded_from_saved"]
        assert service.manifest(run)["read_only"]
        observed = service.traces(run, "adc.code", 0, run.steps, 40, "instrument")
        assert len(observed["values"]) <= 40
        assert observed["missing_count"] > 0
        assert sum(observed["recorded_counts"]) == sum(
            b.sample_count for b in read_all(source / "observable/raw.zlraw").blocks
        )
        assert service.lineage(run, 100)["status"] == "not_recorded"
        assert service.replay(run, 0, 10, [], "instrument")["status"] == "not_computed"
    assert hashes(source) == before


def test_saved_replay_matches_original_codes_and_never_modifies_source(tmp_path):
    cfg = demo_config(seed=21)
    source = tmp_path / "run"
    run_physical(source, cfg, 2, save_checkpoints=True)
    before = hashes(source)
    with saved_service(source) as service:
        run = service.runs["saved"]
        r = service.replay(run, 11000, 100, ["power_w"])
        expected = np.load(source / "truth/source_codes.npy", allow_pickle=False).reshape(-1)
        assert (
            r["codes"] == expected[r["first_sample"] : r["first_sample"] + len(r["codes"])].tolist()
        )
        assert r["status"] == "replayed"
        signals = service.signals(run, "research")["signals"]
        assert (
            next(s for s in signals if s["signal_id"] == "adc.code")["visibility"] == "ORACLE_ONLY"
        )
        signals = service.signals(run, "instrument")["signals"]
        assert (
            next(s for s in signals if s["signal_id"] == "adc.code")["visibility"] == "OBSERVABLE"
        )
        assert service.state(run, 11000, {"power_w"})["status"] == "replayed"
        assert service.state(run, 11000, {"power_w"}, "instrument")["status"] == "not_recorded"
        assert service.events(run, 0, run.steps, None)["status"] == "not_recorded"
    assert hashes(source) == before


def test_corrupt_fingerprinted_source_is_refused(tmp_path):
    source = tmp_path / "run"
    run_physical(source, demo_config(), 1, save_checkpoints=True)
    p = source / "truth/source_codes.npy"
    body = bytearray(p.read_bytes())
    body[-1] ^= 1
    p.write_bytes(body)
    with pytest.raises(ValueError, match="hash|SHA"):
        with saved_service(source):
            pass


def test_no_saved_checkpoints_are_not_replaced_by_fabricated_start_state(tmp_path):
    source = tmp_path / "run"
    run_physical(source, demo_config(), 1)
    with saved_service(source) as service:
        r = service.runs["saved"]
        assert service.state(r, 0, {"power_w"})["status"] == "not_recorded"
        assert service.replay(r, 0, 10, [])["status"] == "not_recorded"


def test_legacy_preset_hash_can_load_but_unknown_custom_hash_cannot(tmp_path):
    source = tmp_path / "run"
    run_physical(source, demo_config(), 1, save_checkpoints=True)
    p = source / "run.json"
    meta = json.loads(p.read_text())
    meta.pop("workbench")
    p.write_text(json.dumps(meta))
    with saved_service(source) as service:
        assert (
            service.manifest(service.runs["saved"])["artifact_integrity"]
            == "legacy_unfingerprinted"
        )
    meta["config_hash"] = "not-a-known-preset"
    p.write_text(json.dumps(meta))
    with pytest.raises(ValueError):
        with saved_service(source):
            pass


def test_mmap_change_after_loading_is_refused(tmp_path):
    source = tmp_path / "run"
    run_physical(source, demo_config(), 1, save_checkpoints=True)
    with saved_service(source) as service:
        p = source / "truth/source_codes.npy"
        with p.open("r+b") as f:
            f.seek(-1, 2)
            f.write(b"X")
        with pytest.raises(ValueError, match="changed"):
            service.traces(service.runs["saved"], "adc.code", 0, 100, 20)


def test_cli_passes_saved_path_and_rejects_ambiguous_selection(tmp_path, monkeypatch):
    from zlscan.cli import main
    from zlscan.engine import run_service

    called = []
    monkeypatch.setattr(run_service, "serve", lambda *args: called.append(args))
    assert main(["serve", "--run", str(tmp_path), "--port", "0"]) == 0
    assert called[0][3] == tmp_path
    with pytest.raises(SystemExit):
        main(["serve", "--run", str(tmp_path), "--images", "images.json"])


def test_actual_http_has_source_visibility_gaps_and_saved_partial_timestamps(tmp_path):
    import http.client
    import threading

    from zlscan.api.server import make_server

    source = tmp_path / "run"
    cfg = demo_config(seed=73)
    run_physical(source, cfg, 2, digital={"latency_half_cycles": 6}, save_checkpoints=True)
    before = hashes(source)
    partial = json.loads((source / "truth/link_lineage.json").read_text())["records"][0]
    with saved_service(source) as service:
        server = make_server(service, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def get(path):
            c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=20)
            try:
                c.request("GET", path)
                r = c.getresponse()
                assert r.status == 200
                return json.loads(r.read())
            finally:
                c.close()

        try:
            m = get("/runs/saved/manifest")
            assert m["loaded_from_saved"] and m["read_only"]
            b = get(
                "/runs/saved/traces?signal=adc.code&start=0&end=20000&pixel_width=40&view=instrument"
            )
            assert b["visibility"] == "OBSERVABLE" and b["missing_count"] > 0
            b = get(
                "/runs/saved/traces?signal=adc.code&start=0&end=20000&pixel_width=40&view=research"
            )
            assert b["visibility"] == "ORACLE_ONLY"
            index = partial["sample_index"]
            b = get(f"/runs/saved/samples/{index}/lineage?view=research")
            assert b["lineage"]["adc_output_time"] == str(partial["adc_output_time"])
            assert b["lineage"]["host_arrival_time"] is None
            assert (
                get(f"/runs/saved/samples/{index}/lineage?view=instrument")["status"]
                == "not_recorded"
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
    assert before == hashes(source)


def test_custom_scene_timeline_and_injection_round_trip(tmp_path):
    from zlscan.engine.chain import Phase
    from zlscan.truth.scene import Scene

    cfg = demo_config(seed=28)
    cfg.scenes["other"] = Scene.from_density(np.array([[0.2, 1.2], [1.5, 0.5]]), 0.08, 0.2)
    cfg.timeline = [Phase("first", 0, 1, "main"), Phase("second", 1, 1, "other")]
    cfg.injections = [(333, 1, 0.0002)]
    source = tmp_path / "run"
    run_physical(source, cfg, 2, save_checkpoints=True)
    with saved_service(source) as service:
        r = service.runs["saved"]
        assert r.cfg.hash() == cfg.hash()
        np.testing.assert_array_equal(
            r.cfg.scenes["other"].transmittance_map, cfg.scenes["other"].transmittance_map
        )
        replay = service.replay(r, 11000, 100, ["power_w"])
        expected = np.load(source / "truth/source_codes.npy", allow_pickle=False).reshape(-1)
        assert (
            replay["codes"]
            == expected[
                replay["first_sample"] : replay["first_sample"] + len(replay["codes"])
            ].tolist()
        )


def test_cli_replay_uses_saved_custom_configuration_without_preset(tmp_path):
    from zlscan.cli import main

    cfg = demo_config(seed=63)
    cfg.source = replace(cfg.source, power_w=0.00072)
    source = tmp_path / "run"
    run_physical(source, cfg, 2, save_checkpoints=True)
    assert (
        main(
            [
                "replay",
                "--run",
                str(source),
                "--start-step",
                "11000",
                "--steps",
                "100",
                "--out",
                str(tmp_path / "replayed"),
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "replay",
                "--run",
                str(source),
                "--preset",
                "demo",
                "--start-step",
                "11000",
                "--steps",
                "100",
                "--out",
                str(tmp_path / "bad"),
            ]
        )
        == 1
    )
    assert not (tmp_path / "bad").exists()
