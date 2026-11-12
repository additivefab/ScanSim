import csv
import json
import xml.etree.ElementTree as ET

import pytest

from zlscan.experiment.sweep import run_sweep
from zlscan.metrics.compare_runs import compare_runs


def test_sweep_writes_one_complete_run_per_cartesian_point(tmp_path):
    result = run_sweep(
        tmp_path / "sweep",
        preset="demo",
        parameter_values={"seed": [1, 2], "source.power_w": [1e-3, 2e-3]},
        lines=2,
    )
    assert len(result["points"]) == 4
    for point in result["points"]:
        run = json.loads(
            (tmp_path / "sweep" / f"point-{point['index']:04d}" / "run.json").read_text()
        )
        assert run["integrity"] == "complete"
    summary = tmp_path / "sweep" / "summary.csv"
    with summary.open() as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 4
    assert {float(row["source.power_w"]) for row in rows} == {1e-3, 2e-3}
    svg = ET.parse(tmp_path / "sweep" / "curves.svg")
    assert len(svg.findall(".//{http://www.w3.org/2000/svg}circle")) == 16


@pytest.mark.parametrize(
    "axes,preset",
    [
        ({"source.missing": [1]}, "demo"),
        ({"seed": [1.5]}, "demo"),
        ({"source.power_w": [float("nan")]}, "demo"),
        ({"source.power_w": [True]}, "demo"),
        ({"evidence": [1]}, "demo"),
        ({"seed": []}, "demo"),
        ({"seed": [1]}, "unknown"),
        ({"seed": [1], "fs_hz": [1], "os": [2]}, "demo"),
    ],
)
def test_sweep_rejects_invalid_axes_before_writing(tmp_path, axes, preset):
    output = tmp_path / "invalid"
    with pytest.raises(ValueError):
        run_sweep(output, preset=preset, parameter_values=axes, lines=2)
    assert not output.exists()


def test_sweep_preserves_existing_output(tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("original")
    with pytest.raises(ValueError, match="already exists"):
        run_sweep(output, preset="demo", parameter_values={"seed": [1]}, lines=2)
    assert marker.read_text() == "original"


def test_compare_reports_manifest_and_raw_stream_identity(tmp_path):
    from zlscan.config.presets import demo_config
    from zlscan.engine.physical_run import run_physical

    run_physical(tmp_path / "a", demo_config(), 2)
    run_physical(tmp_path / "b", demo_config(), 2)
    result = compare_runs(tmp_path / "a", tmp_path / "b")
    assert result["raw"]["equal"]
    assert result["manifest_differences"] == {}


def test_compare_distinguishes_missing_artifacts_from_equal_outputs(tmp_path):
    for name in ("a", "b"):
        run = tmp_path / name
        run.mkdir()
        (run / "run.json").write_text(json.dumps({"seed": 1 if name == "a" else 2}))
    result = compare_runs(tmp_path / "a", tmp_path / "b")
    assert result["raw"]["equal"] is None
    assert result["raw"]["status"] == "missing_artifact"
    assert result["manifest_differences"]["seed"] == {"first": 1, "second": 2}


def test_compare_cli_preserves_input_manifest(tmp_path):
    from zlscan.cli import main

    run = tmp_path / "run"
    run.mkdir()
    manifest = run / "run.json"
    manifest.write_text("{}")
    assert main(["compare", "--first", str(run), "--second", str(run), "--out", str(manifest)]) != 0
    assert manifest.read_text() == "{}"
