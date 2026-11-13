"""Distinct working points inside one observed row must linearize before merging."""

import json

import numpy as np
import pytest
from tests.unit.test_segmented_rows import fixture

from zlscan.calib.file import load_calibration, save_calibration
from zlscan.contracts.calibration import (
    CalibItem,
    CalibrationSet,
    CaptureDescription,
    EpochBinding,
    Provenance,
    resolve_calibrations,
)
from zlscan.io.image_artifact import ImageArtifact
from zlscan.recon.capture import reconstruct_capture
from zlscan.recon.density import Grid


def working_points(tmp_path, **kwargs):
    raw, old, base = fixture(tmp_path, **kwargs)
    low = base.model_copy(deep=True)
    low.calibration_id = "low"
    low.binds_to.epoch_range = (0, 0)
    high = base.model_copy(deep=True)
    high.calibration_id = "high"
    high.binds_to.epoch_range = (1, 1)
    high.binds_to.simulation_hash = "high-gain-config"
    high.binds_to.gain_settings = {"0": "high_assumed"}
    high.items["white_raw_code"].value["0"]["median"] = [30] * 6
    capture = CaptureDescription(
        version=2,
        raw_sha256=old.raw_sha256,
        binding=old.binding,
        lines=1,
        epoch_bindings=[
            EpochBinding(epoch=0, binding=low.binds_to.model_copy(deep=True)),
            EpochBinding(epoch=1, binding=high.binds_to.model_copy(deep=True)),
        ],
    )
    calibrations = CalibrationSet(calibration_set_id="two-points", calibrations=[high, low])
    return raw, capture, calibrations


def test_set_roundtrip_and_different_binding_selection(tmp_path):
    _, capture, calibrations = working_points(tmp_path)
    path = tmp_path / "calibrations.json"
    save_calibration(path, calibrations)
    got = load_calibration(path)
    assert isinstance(got, CalibrationSet)
    assert {k: v.calibration_id for k, v in resolve_calibrations(got, capture, [0, 1]).items()} == {
        0: "low",
        1: "high",
    }


@pytest.mark.parametrize("failure", ["missing", "ambiguous", "model", "gain", "capture_epoch"])
def test_invalid_selection_refuses_before_output(tmp_path, failure):
    raw, capture, calibrations = working_points(tmp_path)
    if failure == "missing":
        calibrations.calibrations.pop(0)
    elif failure == "ambiguous":
        duplicate = calibrations.calibrations[0].model_copy(deep=True)
        duplicate.calibration_id = "another-high"
        calibrations.calibrations.append(duplicate)
    elif failure == "model":
        calibrations.calibrations[0].provenance = Provenance.MODEL
    elif failure == "gain":
        capture.epoch_bindings[1].binding.gain_settings = {"0": "other"}
    else:
        capture.epoch_bindings.pop()
    output = tmp_path / "image"
    with pytest.raises(ValueError):
        reconstruct_capture(raw, capture, calibrations, Grid(6, 1, 1, 1), output)
    assert not output.exists()


def test_same_row_uses_its_own_white_reference_and_preserves_calibration_lineage(tmp_path):
    raw, capture, calibrations = working_points(tmp_path)
    output = tmp_path / "image"
    result = reconstruct_capture(raw, capture, calibrations, Grid(6, 1, 1, 1), output)
    signal = np.interp([10, 12, 14, 16, 18, 20], [0, 10, 20, 30], [0.01, 0.1, 1, 10])
    expected = (signal - 0.01) / np.array([0.99, 0.99, 9.99, 9.99, 9.99, 9.99])
    with np.load(output / "density.npz") as data:
        np.testing.assert_allclose(data["transmittance"][0], expected)
        np.testing.assert_allclose(data["density"][0], -np.log10(expected))
        assert not data["flags"].any()
    assert result["epoch_calibrations"] == {"0": "low", "1": "high"}
    assert result["epoch_coordinate_sources"]["0"]["spatial_model_available"] is False
    assert result["epoch_coordinate_sources"]["1"]["delay_calibration_used"] is False
    with ImageArtifact(output, raw_path=raw) as artifact:
        assert artifact.pixel_lineage(0, 0)["samples"][0]["calibration_id"] == "low"
        assert artifact.pixel_lineage(0, 2)["samples"][0]["calibration_id"] == "high"
        assert (
            artifact.pixel_lineage(0, 2)["samples"][0]["coordinate_source"]
            == "declared_uniform_grid"
        )
    plan = json.loads((output / "lineage.json").read_text())
    assert plan["format"] == "zlscan-image-lineage/3"


def test_epoch_delay_moves_only_its_own_segment_before_linear_merge(tmp_path):
    raw, capture, calibrations = working_points(tmp_path)
    calibrations.calibrations[0].items["delay"] = CalibItem(
        value={"delay_ticks": 1000000, "channel": 0}
    )
    output = tmp_path / "image"
    reconstruct_capture(raw, capture, calibrations, Grid(6, 1, 1, 1), output)
    expected = (((0.28 - 0.01) / 0.99) + ((0.46 - 0.01) / 9.99)) / 2
    with np.load(output / "density.npz") as data:
        assert data["transmittance"][0, 1] == pytest.approx(expected)
    with ImageArtifact(output) as artifact:
        samples = artifact.pixel_lineage(0, 1)["samples"]
        assert {s["calibration_id"] for s in samples} == {"low", "high"}
        assert {s["sample_index"] for s in samples} == {"101", "102"}
        assert [s["normalized_weight"] for s in samples] == [0.5, 0.5]


def test_epoch_binding_rejects_duplicate_or_mismatched_ranges(tmp_path):
    _, capture, _ = working_points(tmp_path)
    document = capture.model_dump(mode="json")
    document["epoch_bindings"].append(document["epoch_bindings"][0])
    with pytest.raises(ValueError):
        CaptureDescription.model_validate(document)
    document["epoch_bindings"].pop()
    document["epoch_bindings"][1]["binding"]["epoch_range"] = [0, 1]
    with pytest.raises(ValueError):
        CaptureDescription.model_validate(document)


@pytest.mark.parametrize("case", ["gap", "orphan", "reset"])
def test_unavailable_positions_remain_invalid_and_sources_are_unchanged(tmp_path, case):
    from zlscan.contracts.flags import InvalidFlag
    from zlscan.io.manifest import sha256_file

    raw, capture, cals = working_points(tmp_path, **{case: True})
    before = sha256_file(raw)
    output = tmp_path / "image"
    result = reconstruct_capture(raw, capture, cals, Grid(6, 1, 1, 1), output)
    with np.load(output / "density.npz") as data:
        absent = range(2, 4) if case == "gap" else range(6) if case == "orphan" else range(2, 6)
        assert np.isnan(data["density"][0, list(absent)]).all()
        assert (data["flags"][0, list(absent)] & int(InvalidFlag.MISSING_SAMPLE)).all()
    assert sha256_file(raw) == before
    assert bool(result["unplaced_blocks"]) == (case != "gap")


def test_common_single_header_and_set_have_identical_arrays(tmp_path):
    raw, capture, cal = fixture(tmp_path)
    one = CalibrationSet(calibration_set_id="common", calibrations=[cal])
    reconstruct_capture(raw, capture, cal, Grid(6, 1, 1, 1), tmp_path / "old")
    reconstruct_capture(raw, capture, one, Grid(6, 1, 1, 1), tmp_path / "set")
    with np.load(tmp_path / "old/density.npz") as old, np.load(tmp_path / "set/density.npz") as new:
        assert old.files == new.files
        for name in old.files:
            np.testing.assert_array_equal(old[name], new[name])


def test_quality_flags_are_bound_to_the_original_epoch_sample(tmp_path):
    from zlscan.contracts.flags import InvalidFlag

    raw, capture, cals = working_points(tmp_path)
    event = dict(
        kind="front_end_clipped",
        origin="SIMULATED",
        visibility="OBSERVABLE",
        derivation="SENSOR",
        capability_ref="fixture/clip",
        clock_domain=1,
        count=104,
        tick=104000000,
        session_id=19,
        epoch=1,
        related_sample=["sample", "main", 19, 1, 1, 0, 104],
    )
    quality = tmp_path / "quality.json"
    quality.write_text(json.dumps(dict(raw_sha256=capture.raw_sha256, ranges=[dict(event=event)])))
    reconstruct_capture(
        raw, capture, cals, Grid(6, 1, 1, 1), tmp_path / "image", quality_path=quality
    )
    with np.load(tmp_path / "image/density.npz") as data:
        assert data["flags"][0, 4] & int(InvalidFlag.FRONT_END_CLIPPED)
        assert not data["flags"][0, :4].any()
    event["visibility"] = "ORACLE_ONLY"
    quality.write_text(json.dumps(dict(raw_sha256=capture.raw_sha256, ranges=[dict(event=event)])))
    with pytest.raises(ValueError):
        reconstruct_capture(
            raw, capture, cals, Grid(6, 1, 1, 1), tmp_path / "rejected", quality_path=quality
        )
    assert not (tmp_path / "rejected").exists()


def test_capture_v1_serialization_remains_readable_by_the_original_schema(tmp_path):
    _, capture, _ = fixture(tmp_path)
    assert "epoch_bindings" not in capture.model_dump()
    assert "epoch_bindings" not in json.loads(capture.model_dump_json())


def test_v2_uses_epoch_hashes_without_fabricating_a_global_configuration(tmp_path):
    raw, capture, cals = working_points(tmp_path)
    capture.binding.simulation_hash = None
    capture.binding.gain_settings = {}
    result = reconstruct_capture(raw, capture, cals, Grid(6, 1, 1, 1), tmp_path / "image")
    assert result["epoch_calibrations"] == {"0": "low", "1": "high"}


def geometric_capture(tmp_path, *, within_line=False):
    from zlscan.io.manifest import sha256_file
    from zlscan.io.rawstream import (
        FLAG_EPOCH_START,
        FLAG_LINE_END,
        FLAG_LINE_START,
        RawWriter,
        read_all,
    )

    raw, capture, cals = working_points(tmp_path)
    session = read_all(raw).session.model_copy(deep=True)
    session.windows.update(sync_latency_cycles=0, window_start=0)
    path = tmp_path / "geometric.zlraw"
    with RawWriter(path, session) as writer:
        if within_line:
            writer.write_block(
                epoch=0,
                channel_mask=1,
                clock_domain=1,
                first_sample_index=100,
                samples=np.array([[10], [12]], np.uint16),
                flags=FLAG_LINE_START | FLAG_EPOCH_START,
            )
            writer.write_block(
                epoch=1,
                channel_mask=1,
                clock_domain=1,
                first_sample_index=102,
                samples=np.array([[14], [16], [18], [20]], np.uint16),
                flags=FLAG_LINE_END | FLAG_EPOCH_START,
            )
        else:
            for epoch in (0, 1):
                writer.write_block(
                    epoch=epoch,
                    line_number=epoch,
                    channel_mask=1,
                    clock_domain=1,
                    first_sample_index=100 + epoch * 100,
                    samples=np.array([10, 12, 14, 16, 18, 20], np.uint16)[:, None],
                    flags=FLAG_LINE_START | FLAG_LINE_END | FLAG_EPOCH_START,
                )
    capture.raw_sha256 = sha256_file(path)
    capture.lines = 1 if within_line else 2
    policy = dict(
        facets=1, period_ticks=100000000, tolerance_ticks=0, phase_reference="fixture/facet_sensor"
    )
    timing = dict(raw_sha256=capture.raw_sha256, policy=policy, lines=[])
    for epoch in (0,) if within_line else (0, 1):
        event = dict(
            origin="SIMULATED",
            visibility="OBSERVABLE",
            derivation="SENSOR",
            capability_ref="fixture/facet_sensor",
            session_id=19,
            epoch=epoch,
            clock_domain=1,
            tick=(100 + epoch * 100) * 1000000,
            count=100 + epoch * 100,
        )
        timing["lines"].append(
            dict(
                line=epoch,
                sync=dict(kind="line_sync", **event),
                anchor=dict(kind="facet_index", payload=dict(phase=0), **event),
            )
        )
    timing_path = tmp_path / "facets.json"
    timing_path.write_text(json.dumps(timing))
    for cal in cals.calibrations:
        high = cal.calibration_id == "high"
        fit = dict(
            x_coefficients_m=[2 if high else 3, 3, 0],
            x_residual_rms_m=0,
            support_u=[-1, 1],
            y_offset_m=-1 if high else 0,
        )
        cal.items["facet_geometry"] = CalibItem(
            value=dict(policy=policy, window=session.windows, facets={"0": fit})
        )
        cal.items["facet_gain"] = CalibItem(value={"channels": {"0": {"0": 2 if high else 1}}})
    return path, capture, cals, timing_path


def test_epoch_geometry_and_gain_use_their_own_xy_and_do_not_double_apply_delay(tmp_path):
    raw, capture, cals, timing = geometric_capture(tmp_path)
    cals.calibrations[0].items["delay"] = CalibItem(value={"delay_ticks": 99000000, "channel": 0})
    output = tmp_path / "image"
    reconstruct_capture(raw, capture, cals, Grid(6, 2, 1, 1), output, facet_path=timing)
    expected = (((0.1 - 0.01) / 0.99) + ((0.28 - 0.01) / 9.99 / 2)) / 2
    with np.load(output / "density.npz") as data:
        assert data["transmittance"][0, 0] == pytest.approx(expected)
        assert np.isnan(data["density"][1]).all()
    with ImageArtifact(output) as artifact:
        samples = artifact.pixel_lineage(0, 0)["samples"]
        assert {s["sample_index"] for s in samples} == {"100", "201"}
        assert {s["calibration_id"] for s in samples} == {"low", "high"}


@pytest.mark.parametrize("collection", [True, False])
def test_geometry_cannot_borrow_phase_across_an_epoch_change_inside_a_line(tmp_path, collection):
    from zlscan.contracts.flags import InvalidFlag

    raw, capture, cals, timing = geometric_capture(tmp_path, within_line=True)
    if collection:
        cal = cals
    else:
        cal = cals.calibrations[1].model_copy(deep=True)
        cal.binds_to = capture.binding.model_copy(deep=True)
        capture = CaptureDescription(
            raw_sha256=capture.raw_sha256, binding=capture.binding, lines=1
        )
    output = tmp_path / "image"
    reconstruct_capture(raw, capture, cal, Grid(6, 1, 1, 1), output, facet_path=timing)
    with np.load(output / "density.npz") as data:
        assert (data["flags"][0, 2:] & int(InvalidFlag.FACET_UNRESOLVED)).all()
        assert np.isnan(data["density"][0, 2:]).all()


def test_public_collection_cli_builds_a_self_contained_file_and_refuses_duplicates(tmp_path):
    from zlscan.cli import main

    _, _, cals = working_points(tmp_path)
    inputs = []
    for cal in cals.calibrations:
        path = tmp_path / (cal.calibration_id + ".json")
        save_calibration(path, cal)
        inputs += ["--calibration", str(path)]
    output = tmp_path / "collection.json"
    assert main(["calibration-set", "--id", "public-set", *inputs, "--out", str(output)]) == 0
    result = load_calibration(output)
    assert result.calibration_set_id == "public-set"
    assert [c.calibration_id for c in result.calibrations] == ["high", "low"]
    before = output.read_bytes()
    assert main(["calibration-set", "--id", "overwrite", *inputs, "--out", str(output)]) == 1
    assert output.read_bytes() == before
    duplicate = tmp_path / "duplicate.json"
    assert (
        main(
            [
                "calibration-set",
                "--id",
                "bad",
                "--calibration",
                inputs[1],
                "--calibration",
                inputs[1],
                "--out",
                str(duplicate),
            ]
        )
        == 1
    )
    assert not duplicate.exists()


def test_lineage_refuses_a_segment_relabelled_with_another_working_point(tmp_path):
    from zlscan.io.manifest import sha256_file

    raw, capture, cals = working_points(tmp_path)
    output = tmp_path / "image"
    reconstruct_capture(raw, capture, cals, Grid(6, 1, 1, 1), output)
    path = output / "lineage.json"
    plan = json.loads(path.read_text())
    plan["lines"][0]["segments"][1]["calibration_id"] = "low"
    path.write_text(json.dumps(plan))
    run = json.loads((output / "run.json").read_text())
    run["lineage_sha256"] = sha256_file(path)
    (output / "run.json").write_text(json.dumps(run))
    with pytest.raises(ValueError, match="epoch lineage"):
        ImageArtifact(output)


def test_collection_requires_observed_epochs_and_shared_session_identity(tmp_path):
    raw, capture, cals = working_points(tmp_path)
    with pytest.raises(ValueError, match="ZLRAW"):
        reconstruct_capture(
            raw, capture, cals, Grid(6, 1, 1, 1), tmp_path / "headerless", headerless_spp=1
        )
    capture.epoch_bindings[1].binding.register_dump_hash = "unrecorded-change"
    with pytest.raises(ValueError, match="single-session"):
        reconstruct_capture(raw, capture, cals, Grid(6, 1, 1, 1), tmp_path / "different-registers")
    assert not (tmp_path / "different-registers").exists()


def test_actual_offline_js_uses_each_segment_geometry_and_calibration_id(tmp_path):
    import re
    import shutil
    import subprocess

    from zlscan.report.image_view import _image_payload

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js needed for actual JS helpers; not a browser test")
    raw, capture, cals, timing = geometric_capture(tmp_path)
    output = tmp_path / "image"
    reconstruct_capture(raw, capture, cals, Grid(6, 2, 1, 1), output, facet_path=timing)
    payload = _image_payload(output, raw)
    html = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "src/zlscan/report/image_view.html"
    ).read_text(encoding="utf-8")
    helper = re.search(r"function sourceTicks\(.*?(?=function lineage\()", html, re.S).group()
    script = (
        "const HELPER="
        + json.dumps(helper)
        + ";const IMAGE="
        + json.dumps(payload)
        + r""";
const vm=require('node:vm'),assert=require('node:assert/strict');
const context=vm.createContext({Math,BigInt});vm.runInContext(HELPER,context);
const text=context.plannedLineage(IMAGE,0,0,0);
assert(text.includes('样本 100')&&text.includes('样本 201'));
assert(text.includes('标定 low')&&text.includes('标定 high'));
assert(!text.includes('样本 200'));
"""
    )
    result = subprocess.run(
        [node], input=script, capture_output=True, text=True, encoding="utf-8", timeout=20
    )
    assert result.returncode == 0, result.stderr


def test_image_binding_comparison_can_check_the_entire_epoch_description(tmp_path):
    from zlscan.contracts.flags import InvalidFlag

    raw, capture, cals = working_points(tmp_path)
    output = tmp_path / "image"
    reconstruct_capture(raw, capture, cals, Grid(6, 1, 1, 1), output)
    expected = capture.model_dump(mode="json")
    expected["epoch_bindings"].reverse()
    with ImageArtifact(output, expected_binding=expected) as image:
        assert image.manifest()["calibration_status"] == "compatible_at_reconstruction"
        assert image.manifest()["epoch_calibrations"] == {"0": "low", "1": "high"}
    expected["epoch_bindings"][0]["binding"]["gain_settings"] = {"0": "another-gain"}
    with ImageArtifact(output, expected_binding=expected) as image:
        assert image.manifest()["calibration_status"] == "CALIBRATION_MISMATCH"
        tile = image.tile(0, 0)
        assert all(value is None for value in tile["density"][0])
        assert all(flag & int(InvalidFlag.CALIBRATION_MISMATCH) for flag in tile["flags"][0])


def test_many_vertical_working_points_keep_a_bounded_output_row_cache(tmp_path):
    import weakref

    from tests.unit.test_density_reconstruction import calibration

    from zlscan.recon.density import reconstruct_density
    from zlscan.recon.disk_density import DiskDensity

    width = 100
    cal = calibration()
    cal.items["dark_raw_code"].value["0"]["median"] = [0] * width
    cal.items["white_raw_code"].value["0"]["median"] = [20] * width
    grid = Grid(2, 100, 1, 1)
    x = np.full(width, 0.75)
    y = np.arange(width) * 0.6 + 0.5

    class Tracked(DiskDensity):
        refs = []
        peak = 0

        def _row(self, index):
            result = super()._row(index)
            self.refs.append(weakref.ref(result))
            self.peak = max(self.peak, sum(r() is not None for r in self.refs))
            return result

    scratch_dir = tmp_path / "scratch"
    scratch_dir.mkdir()
    output = tmp_path / "image"
    with (scratch_dir / "rows.bin").open("w+b") as scratch:
        disk = Tracked(scratch, grid, cal, 0, width)
        disk.add_linear(np.ones(width), np.zeros(width, np.uint16), x, y)
        assert disk.peak <= 4, f"{disk.peak} full output rows were resident together"
        disk.write(scratch_dir, output)
    expected = reconstruct_density(
        np.full((1, width), 20, np.uint16),
        x[None, :],
        y[None, :],
        grid,
        cal,
        cal.binds_to,
        channel=0,
    )
    with np.load(output / "density.npz") as actual:
        for name in actual.files:
            np.testing.assert_array_equal(actual[name], getattr(expected, name))
