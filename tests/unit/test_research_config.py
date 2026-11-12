"""One explicitly assumed scenario must drive budgets, runs, reloads and replay."""

import hashlib
import json

import numpy as np
import pytest

from zlscan.cli import main


def scenario(tmp_path, **changes):
    data = {
        "format": "zlscan.research-scenario/1",
        "model_version": "chain-0.3",
        "preset": "nominal",
        "lines": 3,
        "chain": {
            "seed": 91,
            "source": {"power_w": 0.00073},
            "detector": {"shot_noise": False},
            "psf": {"nodes_x": 3, "nodes_y": 3},
        },
    }
    data.update(changes)
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps(data))
    return path


def test_custom_run_budget_and_saved_replay_use_one_configuration(tmp_path):
    from zlscan.engine.chain import Chain
    from zlscan.engine.research_config import load_scenario, scenario_budget
    from zlscan.engine.saved_service import saved_service

    path = scenario(tmp_path)
    original = path.read_bytes()
    loaded = load_scenario(path)
    assert loaded.primary.seed == 91 and loaded.primary.source.power_w == 0.00073
    assert loaded.primary.afe.aa_poles_hz == (2e6,)
    budget = scenario_budget(loaded)
    assert budget["inputs"]["laser.power_w"]["value"] == loaded.primary.source.power_w
    assert budget["inputs"]["adc.actual_conversion_rate_hz"]["value"] == loaded.primary.fs_hz
    assert budget["inputs"]["budget.ny"]["value"] == 3
    # This declared clear-field power exceeds the preset anode limit: preserve the failure.
    assert main(["budget", "--config", str(path), "--out", str(tmp_path / "budget")]) == 1
    b = json.loads((tmp_path / "budget/budget.json").read_text())
    assert b["scenario_configuration"]["primary_hash"] == loaded.primary.hash()
    assert b["flagged_assumed_scenario"]
    assert any(c["id"] == "photons.anode.D0" and c["status"] == "FAIL" for c in b["conclusions"])
    assert (
        main(["run", "--mode", "physical", "--config", str(path), "--out", str(tmp_path / "run")])
        == 0
    )
    r = json.loads((tmp_path / "run/run.json").read_text())
    assert r["config_hash"] == loaded.primary.hash()
    assert r["scenario_configuration"]["primary_hash"] == r["config_hash"]
    assert (tmp_path / "run/inputs/scenario.json").read_bytes() == original
    with saved_service(tmp_path / "run") as service:
        saved = service.runs["saved"]
        assert saved.cfg.hash() == loaded.primary.hash()
        np.testing.assert_array_equal(
            np.load(tmp_path / "run/truth/source_codes.npy").reshape(-1),
            Chain(loaded.primary).advance(30000).codes,
        )
    assert (
        main(
            [
                "replay",
                "--run",
                str(tmp_path / "run"),
                "--start-step",
                "11000",
                "--steps",
                "100",
                "--out",
                str(tmp_path / "replay"),
            ]
        )
        == 0
    )
    assert path.read_bytes() == original


def test_density_scene_and_timeline_roundtrip(tmp_path):
    from zlscan.engine.research_config import load_scenario

    path = scenario(
        tmp_path,
        scenes={
            "a": {
                "width_m": 0.08,
                "height_m": 0.2,
                "off_film_T": 0.0,
                "density": [[0.0, 1.0], [2.0, 3.0]],
            }
        },
        timeline=[
            {"name": "dark", "first_line": 0, "n_lines": 1, "scene": "a", "laser_on": False},
            {"name": "sample", "first_line": 1, "n_lines": 2, "scene": "a", "laser_on": True},
        ],
    )
    cfg = load_scenario(path).primary
    np.testing.assert_allclose(cfg.scenes["a"].transmittance_map, [[1, 0.1], [0.01, 0.001]])
    assert cfg.scenes["a"].off_film_T == 0
    assert not cfg.timeline[0].laser_on


def test_relative_npy_scene_is_hash_verified_and_snapshotted(tmp_path):
    from zlscan.engine.research_config import load_scenario

    np.save(tmp_path / "map.npy", np.array([[0.1, 0.3], [0.5, 0.7]]))
    spec = {
        "width_m": 0.08,
        "height_m": 0.2,
        "off_film_T": 1.0,
        "transmittance_npy": "map.npy",
        "sha256": hashlib.sha256((tmp_path / "map.npy").read_bytes()).hexdigest(),
    }
    path = scenario(tmp_path, scenes={"mapped": spec})
    cfg = load_scenario(path).primary
    np.save(tmp_path / "map.npy", np.ones((2, 2)))
    np.testing.assert_equal(cfg.scenes["mapped"].transmittance_map, [[0.1, 0.3], [0.5, 0.7]])
    with pytest.raises(ValueError, match="SHA"):
        load_scenario(path)


@pytest.mark.parametrize(
    "change",
    [
        {"format": "unknown"},
        {"model_version": "unknown"},
        {"preset": "unknown"},
        {"lines": True},
        {"lines": 0},
        {"chain": {"fs_hz": "1000000"}},
        {"chain": {"fs_hz": 1}},
        {"chain": {"os": 3}},
        {"chain": {"scan": {"facets": 0}}},
        {"chain": {"adc": {"bits": 17}}},
        {"chain": {"adc": {"vmax": -1.0}}},
        {"chain": {"source": {"power_w": None}}},
        {"chain": {"source": {"power_w": float("nan")}}},
        {"chain": {"source": {"power_W": 1.0}}},
        {"chain": {"evidence": {"all": "measured"}}},
        {"chain": {"scan": {"facet_gain": [1.0]}}},
        {"extra": True},
        {"timeline": [{"name": "bad", "first_line": 1, "n_lines": 2, "scene": "missing"}]},
        {"secondary": {"fs_hz": 2000000}},
    ],
)
def test_bad_or_ambiguous_scenarios_fail_before_creating_output(tmp_path, change):
    path = scenario(tmp_path, **change)
    out = tmp_path / "refused"
    assert main(["run", "--mode", "physical", "--config", str(path), "--out", str(out)]) != 0
    assert not out.exists()


def test_duplicate_json_keys_are_rejected(tmp_path):
    from zlscan.engine.research_config import load_scenario

    path = scenario(tmp_path)
    path.write_text(path.read_text().replace('"lines": 3', '"lines": 3, "lines": 4'))
    with pytest.raises(ValueError, match="duplicate"):
        load_scenario(path)


@pytest.mark.parametrize("extra", [["--lines", "2"], ["--preset", "demo"], ["--stream"]])
def test_config_cannot_silently_override_cli_settings(tmp_path, extra):
    path = scenario(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["run", "--mode", "physical", "--config", str(path), *extra])
    assert exc.value.code == 2


def test_digital_mode_rejects_research_config(tmp_path):
    path = scenario(tmp_path)
    with pytest.raises(SystemExit):
        main(["run", "--mode", "digital", "--config", str(path)])


def test_dual_config_preserves_both_requested_chains(tmp_path):
    from zlscan.engine.research_config import load_scenario, scenario_budget

    path = scenario(tmp_path, lines=2, secondary={"seed": 92, "source": {"power_w": 0.00041}})
    loaded = load_scenario(path)
    assert loaded.secondary.seed == 92 and loaded.secondary.source.power_w == 0.00041
    assert loaded.primary.hash() != loaded.secondary.hash()
    assert scenario_budget(loaded)["inputs"]["budget.channels"]["value"] == 2
    assert (
        main(["run", "--mode", "physical", "--config", str(path), "--out", str(tmp_path / "dual")])
        == 0
    )
    r = json.loads((tmp_path / "dual/run.json").read_text())
    assert r["scenario_configuration"]["secondary_hash"] == loaded.secondary.hash()
    assert (tmp_path / "dual/inputs/resolved_1/config.json").is_file()


def test_hardware_budget_rejects_assumed_scenario(tmp_path):
    path = scenario(tmp_path)
    assert (
        main(
            [
                "budget",
                "--config",
                str(path),
                "--mode",
                "hardware_constrained",
                "--out",
                str(tmp_path / "refused"),
            ]
        )
        == 1
    )
    assert not (tmp_path / "refused").exists()


def test_archived_scenario_is_self_contained_and_rerunnable(tmp_path):
    from zlscan.engine.research_config import load_scenario

    np.save(tmp_path / "map.npy", np.array([[0.1, 0.3], [0.5, 0.7]]))
    spec = {
        "width_m": 0.08,
        "height_m": 0.2,
        "off_film_T": 1.0,
        "transmittance_npy": "map.npy",
        "sha256": hashlib.sha256((tmp_path / "map.npy").read_bytes()).hexdigest(),
    }
    path = scenario(tmp_path, lines=2, scenes={"mapped": spec})
    assert (
        main(["run", "--mode", "physical", "--config", str(path), "--out", str(tmp_path / "one")])
        == 0
    )
    np.save(tmp_path / "map.npy", np.ones((2, 2)))
    archived = tmp_path / "one/inputs/resolved_scenario.json"
    cfg = load_scenario(archived).primary
    np.testing.assert_equal(cfg.scenes["mapped"].transmittance_map, [[0.1, 0.3], [0.5, 0.7]])
    assert (
        main(
            ["run", "--mode", "physical", "--config", str(archived), "--out", str(tmp_path / "two")]
        )
        == 0
    )
    np.testing.assert_array_equal(
        np.load(tmp_path / "one/truth/source_codes.npy"),
        np.load(tmp_path / "two/truth/source_codes.npy"),
    )


def test_calibration_records_requested_and_effective_configuration(tmp_path):
    path = scenario(tmp_path, lines=2)
    assert (
        main(
            [
                "run",
                "--mode",
                "calibration",
                "--config",
                str(path),
                "--out",
                str(tmp_path / "reference"),
            ]
        )
        == 0
    )
    r = json.loads((tmp_path / "reference/run.json").read_text())
    assert r["fine_steps"] == 120000  # 12 reference lines at 10,000 fine steps/line.
    assert r["scenario_configuration"]["line_count_semantics"] == "per reference target"
    assert r["config_hash"] != r["scenario_configuration"]["primary_hash"]
    protocol = json.loads((tmp_path / "reference/observable/scan_protocol.json").read_text())
    assert len(protocol["targets"]) == 6


def test_existing_output_is_unchanged_when_config_run_refuses(tmp_path):
    path = scenario(tmp_path)
    out = tmp_path / "preserved"
    out.mkdir()
    (out / "sentinel").write_text("original")
    assert main(["run", "--mode", "physical", "--config", str(path), "--out", str(out)]) == 1
    assert list(out.iterdir()) == [out / "sentinel"]
    assert (out / "sentinel").read_text() == "original"
