"""Do not discard non-LINE_START payloads or compress sample gaps across source epochs."""

import json

import numpy as np
import pytest
from tests.unit.test_density_reconstruction import calibration

from zlscan.contracts.calibration import Binding, CaptureDescription
from zlscan.contracts.flags import InvalidFlag
from zlscan.io.image_artifact import ImageArtifact
from zlscan.io.line_image import reconstruct
from zlscan.io.manifest import sha256_file
from zlscan.io.rawstream import (
    FLAG_EPOCH_START,
    FLAG_LINE_END,
    FLAG_LINE_START,
    RawWriter,
    SessionHeader,
)
from zlscan.recon.capture import reconstruct_capture
from zlscan.recon.density import Grid


def fixture(tmp_path, *, gap=False, orphan=False, reset=False):
    session = SessionHeader(
        schema_version="0.1",
        session_id=19,
        created_at="2026-11-13T00:00:00Z",
        hardware_hash=None,
        firmware_hash=None,
        wiring_rev=None,
        register_dump_hash=None,
        calibration_id=None,
        channel_roles={"0": "signal"},
        clock_domains=[dict(id=1, freq_num=1_000_000, freq_den=1, phase_ticks=0)],
        coding="offset_binary",
        bits_per_sample=16,
        bytes_per_sample=2,
        windows=dict(window_length=6),
        origin="SIMULATED",
    )
    path = tmp_path / "raw.zlraw"
    with RawWriter(path, session) as writer:
        if not orphan:
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
            first_sample_index=0 if reset else 104 if gap or orphan else 102,
            samples=np.array(
                [[18], [20]] if gap or orphan else [[14], [16], [18], [20]], np.uint16
            ),
            flags=FLAG_LINE_END | FLAG_EPOCH_START,
        )
    binding = Binding(
        simulation_hash="bench",
        channel_roles={"0": "signal"},
        gain_settings={"0": "fixed_assumed"},
        epoch_range=(0, 1),
    )
    cal = calibration()
    cal.binds_to = binding
    cal.items["dark_raw_code"].value["0"]["median"] = [0] * 6
    cal.items["white_raw_code"].value["0"]["median"] = [20] * 6
    capture = CaptureDescription(raw_sha256=sha256_file(path), binding=binding, lines=1)
    return path, capture, cal


def test_continuations_fill_their_original_columns_and_keep_per_sample_epoch(tmp_path):
    raw, capture, cal = fixture(tmp_path)
    minimal = reconstruct(raw, range(1))
    assert minimal.status == "complete"
    assert minimal.codes[0].tolist() == [[10, 12, 14, 16, 18, 20]]
    output = tmp_path / "image"
    result = reconstruct_capture(raw, capture, cal, Grid(6, 1, 1, 1), output)
    assert result["valid_fraction"] == 1
    with np.load(output / "density.npz") as data:
        expected = (np.array([0.1, 0.28, 0.46, 0.64, 0.82, 1.0]) - 0.01) / 0.99
        np.testing.assert_allclose(data["transmittance"][0], expected)
    plan = json.loads((output / "lineage.json").read_text())
    assert plan["format"] == "zlscan-image-lineage/2"
    assert [p["column_offset"] for p in plan["lines"][0]["segments"]] == [0, 2]
    with ImageArtifact(output, raw_path=raw) as image:
        assert image.timeline_available
        assert image.state(4)["epoch"] == 1
        sample = image.pixel_lineage(0, 4)["samples"][0]
        assert (sample["sample_index"], sample["epoch"], sample["column"]) == ("104", 1, 4)
        trace = image.traces(0, 6, 1)
        assert trace["mixed_epoch"] == [True] and trace["epochs"] == [[0, 1]]


def test_gaps_stay_in_place_and_missing_pixel_has_no_false_source(tmp_path):
    raw, capture, cal = fixture(tmp_path, gap=True)
    minimal = reconstruct(raw, range(1))
    assert minimal.codes[0][0, 4:].tolist() == [18, 20]
    assert np.all(minimal.invalid[0, 2:4] & InvalidFlag.MISSING_SAMPLE)
    output = tmp_path / "image"
    reconstruct_capture(raw, capture, cal, Grid(6, 1, 1, 1), output)
    with ImageArtifact(output, raw_path=raw) as image:
        missing = image.pixel_lineage(0, 2)
        assert not missing["samples"] and missing["missing_lines"] == [0]
        trace = image.traces(0, 6, 20)
        assert trace["values"] == [10, 12, None, None, 18, 20] and trace["missing_count"] == 2
        assert image.tile(0, 0)["density"][0][2] is None


def test_unanchored_or_reset_continuations_are_explicitly_unplaced(tmp_path):
    raw, capture, cal = fixture(tmp_path, orphan=True)
    output = tmp_path / "image"
    result = reconstruct_capture(raw, capture, cal, Grid(6, 1, 1, 1), output)
    assert result["valid_fraction"] == 0 and result["unplaced_blocks"] == 1
    plan = json.loads((output / "lineage.json").read_text())
    assert plan["lines"][0]["segments"][0]["column_offset"] is None
    with ImageArtifact(output, raw_path=raw) as image:
        assert not image.pixel_lineage(0, 4)["samples"]


def test_one_epoch_calibration_cannot_silently_apply_to_the_second_epoch(tmp_path):
    raw, capture, cal = fixture(tmp_path)
    cal.binds_to = cal.binds_to.model_copy(update={"epoch_range": (0, 0)})
    with pytest.raises(ValueError, match="epoch_range"):
        reconstruct_capture(raw, capture, cal, Grid(6, 1, 1, 1), tmp_path / "image")
    assert not (tmp_path / "image").exists()


def test_quality_event_in_continuation_uses_its_original_column_and_oracle_is_refused(tmp_path):
    from zlscan.io.quality import apply_quality_log
    from zlscan.io.rawstream import read_all

    raw, capture, cal = fixture(tmp_path)
    event = dict(
        kind="front_end_clipped",
        origin="SIMULATED",
        visibility="OBSERVABLE",
        derivation="SENSOR",
        capability_ref="fixture/clip_observation",
        clock_domain=1,
        count=104,
        tick=104_000_000,
        session_id=19,
        epoch=1,
        related_sample=["sample", "main", 19, 1, 1, 0, 104],
    )
    quality = tmp_path / "quality.json"
    quality.write_text(json.dumps(dict(raw_sha256=capture.raw_sha256, ranges=[dict(event=event)])))
    flags = reconstruct(raw, range(1)).invalid
    apply_quality_log(quality, capture.raw_sha256, read_all(raw), 0, flags)
    assert np.flatnonzero(flags[0]).tolist() == [4]
    output = tmp_path / "image"
    reconstruct_capture(raw, capture, cal, Grid(6, 1, 1, 1), output, quality_path=quality)
    with np.load(output / "density.npz") as image:
        assert image["flags"][0, 4] & InvalidFlag.FRONT_END_CLIPPED
        assert np.isnan(image["density"][0, 4]) and np.isfinite(image["density"][0, 2])
    event["visibility"] = "ORACLE_ONLY"
    quality.write_text(json.dumps(dict(raw_sha256=capture.raw_sha256, ranges=[dict(event=event)])))
    with pytest.raises(ValueError, match="unobservable"):
        reconstruct_capture(
            raw, capture, cal, Grid(6, 1, 1, 1), tmp_path / "rejected", quality_path=quality
        )
    assert not (tmp_path / "rejected").exists()


def test_source_counter_reset_disables_unified_timeline_instead_of_guessing_offset(tmp_path):
    raw, capture, cal = fixture(tmp_path, reset=True)
    output = tmp_path / "image"
    result = reconstruct_capture(raw, capture, cal, Grid(6, 1, 1, 1), output)
    assert result["unplaced_blocks"] == 1
    with ImageArtifact(output, raw_path=raw) as image:
        assert not image.timeline_available
        assert image.state(0)["status"] == "not_computed"
        assert not image.pixel_lineage(0, 3)["samples"]


def test_offline_report_embeds_actual_segment_plan_not_last_block_shortcut(tmp_path):
    from zlscan.report.image_view import _image_payload

    raw, capture, cal = fixture(tmp_path)
    output = tmp_path / "image"
    reconstruct_capture(raw, capture, cal, Grid(6, 1, 1, 1), output)
    report = _image_payload(output, raw)
    assert report["lineage"] is None
    assert report["lineage_plan"]["format"] == "zlscan-image-lineage/2"
    assert report["lineage_plan"]["lines"][0]["segments"][1]["source"]["epoch"] == 1


def test_explicitly_common_calibration_combines_linear_values_and_labels_mixed_support(tmp_path):
    raw, capture, cal = fixture(tmp_path)
    output = tmp_path / "image"
    reconstruct_capture(raw, capture, cal, Grid(3, 1, 1, 1), output)
    with ImageArtifact(output, raw_path=raw) as image:
        lineage = image.pixel_lineage(0, 0)
        assert lineage["epochs"] == [0, 1] and lineage["mixed_epoch"]
        assert sum(s["normalized_weight"] for s in lineage["samples"]) == pytest.approx(1)
        assert [s["sample_index"] for s in lineage["samples"]] == ["100", "101", "102"]
    with np.load(output / "density.npz") as data:
        trans = (np.array([0.1, 0.28, 0.46]) - 0.01) / 0.99
        assert data["transmittance"][0, 0] == pytest.approx(
            np.dot(trans, [0.75, 0.75, 0.25]) / 1.75
        )


def test_resigned_wrong_segment_offset_is_rejected(tmp_path):
    raw, capture, cal = fixture(tmp_path, gap=True)
    output = tmp_path / "image"
    reconstruct_capture(raw, capture, cal, Grid(6, 1, 1, 1), output)
    plan_path = output / "lineage.json"
    plan = json.loads(plan_path.read_text())
    plan["lines"][0]["segments"][1]["column_offset"] = 2
    plan_path.write_text(json.dumps(plan))
    run_path = output / "run.json"
    run = json.loads(run_path.read_text())
    run["lineage_sha256"] = sha256_file(plan_path)
    run_path.write_text(json.dumps(run))
    with pytest.raises(ValueError, match="anchor"):
        ImageArtifact(output, raw_path=raw)


def test_actual_offline_js_uses_segment_epoch_and_local_sample_offset(tmp_path):
    import re
    import shutil
    import subprocess

    from zlscan.report.image_view import _TEMPLATE, _image_payload

    if shutil.which("node") is None:
        pytest.skip("Node.js needed for actual report helper check")
    raw, capture, cal = fixture(tmp_path)
    output = tmp_path / "image"
    reconstruct_capture(raw, capture, cal, Grid(6, 1, 1, 1), output)
    payload = _image_payload(output, raw)
    helper = re.search(
        r"function sourceTicks\(.*?(?=function render\()", _TEMPLATE.read_text(), re.S
    ).group()
    script = r"""
const vm=require('node:vm'),assert=require('node:assert/strict');
const context=vm.createContext({Math,BigInt});vm.runInContext(HELPER,context);
const text=context.lineage(IMAGE,0,4,4);
assert(text.includes('时期 1 / 时钟 1 / 样本 104 / 104000000 ps'));
assert(!text.includes('样本 102 /'),'local offset in continuation is not global column');
"""
    result = subprocess.run(
        [shutil.which("node")],
        input="const HELPER="
        + json.dumps(helper)
        + ";const IMAGE="
        + json.dumps(payload)
        + ";\n"
        + script,
        encoding="utf-8",
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
