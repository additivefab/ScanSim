"""Native model flags survive control, recovery and read-only research queries."""

import json
from dataclasses import replace

import numpy as np
import pytest
from tests.unit.test_physical_recording import settings

from zlscan.config.presets import legacy_dc_config
from zlscan.engine.chain import Chain
from zlscan.engine.physical_recording import PhysicalRecorder


def test_quality_observer_preserves_between_adc_clip_without_changing_codes_or_state():
    cfg = legacy_dc_config(1.0)
    cfg.afe = replace(cfg.afe, clip_max_v=0.9)
    cfg.detector = replace(cfg.detector, anode_limit_a=1e-4)
    cfg.injections = [(7, 1, 1.0)]
    plain = Chain(cfg)
    observed = Chain(cfg)
    observed.record_quality = True
    a = plain.advance(13)
    b = observed.advance(13)
    assert np.array_equal(a.codes, b.codes) and plain.checkpoint() == observed.checkpoint()
    assert np.flatnonzero(b.quality_flags["front_end_clipped"]).tolist() == [7]
    assert np.flatnonzero(b.quality_flags["pmt_over_limit"]).tolist() == [7]
    assert not b.front_end_clipped.any()  # Fine step 7 is between source ADC edges.
    assert a.quality_flags == {}


def create(path):
    configs, link, acq, stages = settings()
    return PhysicalRecorder(
        path,
        configs,
        link,
        acq,
        stages,
        session_id=19,
        run_id="physical",
        branch_id="main",
        record_quality=True,
    )


def test_quality_participates_in_one_root_resume_and_original_adc_is_unchanged(tmp_path):
    from zlscan.io.model_quality import QualityReader

    configs, link, acq, stages = settings()
    with PhysicalRecorder(
        tmp_path / "plain",
        configs,
        link,
        acq,
        stages,
        session_id=19,
        run_id="physical",
        branch_id="main",
    ) as recorder:
        recorder.advance(173)
        recorder.finish()
        recorder.save_checkpoint()
        expected = {c: x.checkpoint() for c, x in recorder.chains.items()}
    folder = tmp_path / "quality"
    with create(folder) as recorder:
        recorder.advance(19)
        recorder.save_checkpoint()
    with PhysicalRecorder.restore(
        folder, configs, link, acq, stages, (folder / "checkpoint.json").read_bytes()
    ) as recorder:
        recorder.advance(154)
        recorder.finish()
        recorder.save_checkpoint()
        assert {c: x.checkpoint() for c, x in recorder.chains.items()} == expected
    assert (folder / "raw.zlraw").read_bytes() == (tmp_path / "plain/raw.zlraw").read_bytes()
    root = json.loads((folder / "checkpoint.json").read_bytes())
    assert root["format"] == PhysicalRecorder.QUALITY_FORMAT
    with QualityReader(folder / "quality_model.jsonl", root["quality"]) as reader:
        assert reader.step == 173
        assert reader.query(1, 0, 173, {"pmt_over_limit"})["status"] == "exact_record"


def test_failed_quality_write_retains_old_published_root(tmp_path, monkeypatch):
    with create(tmp_path / "run") as recorder:
        recorder.advance(19)
        before = recorder.save_checkpoint().read_bytes()

        def broken(*args):
            raise OSError("quality storage failed")

        monkeypatch.setattr(recorder.quality, "append", broken)
        with pytest.raises(OSError, match="quality storage"):
            recorder.advance(13)
        with pytest.raises(OSError, match="failed"):
            recorder.save_checkpoint()
        assert (recorder.directory / "checkpoint.json").read_bytes() == before


def test_public_cli_events_keep_native_grids_channels_and_instrument_isolation(
    tmp_path, monkeypatch
):
    from tests.unit.test_recording_workbench import hashes, source

    from zlscan.engine.saved_service import saved_service
    from zlscan.io.model_quality import GRIDS

    folder = source(
        tmp_path,
        monkeypatch,
        record_quality=True,
        chain={
            "seed": 3,
            "source": {"gated": False},
            "detector": {"shot_noise": False, "anode_limit_a": 1e-20},
            "afe": {"clip_max_v": -5.0},
            "psf": {"nodes_x": 1, "nodes_y": 1},
        },
        secondary={"seed": 31, "detector": {"anode_limit_a": None}},
    )
    before = hashes(folder)
    with saved_service(folder) as service:
        r = service.runs["recording-c0"]
        result = service.events(r, 7, 13, set(GRIDS), "research")
        assert result["status"] == "exact_record"
        clips = [e for e in result["events"] if e["kind"] == "front_end_clipped"]
        assert [e["step"] for e in clips] == list(range(7, 13))
        assert all(e["sample_grid"] == "fine" and e["source_sample_index"] is None for e in clips)
        adc = [e for e in result["events"] if e["kind"] == "adc_saturated"]
        assert [e["step"] for e in adc] == [8, 12]
        assert service.events(r, 0, 11000, set(GRIDS), "research")["truncated"]
        instrument = service.events(r, 0, 11000, set(GRIDS), "instrument")
        assert instrument["status"] == "not_recorded" and instrument["events"] == []
        other = service.events(
            service.runs["recording-c1"], 0, 11000, {"pmt_over_limit"}, "research"
        )
        assert other["events"] == [] and other["status"] == "exact_record"
    assert hashes(folder) == before


def test_quality_tail_branch_preserves_source_and_rejects_wrong_source_header(tmp_path):
    from tests.unit.test_recording_workbench import hashes

    configs, link, acq, stages = settings()
    folder = tmp_path / "run"
    with create(folder) as recorder:
        recorder.advance(19)
        blob = recorder.save_checkpoint().read_bytes()
        recorder.advance(17)
    before = hashes(folder)
    branch = tmp_path / "branch"
    with PhysicalRecorder.restore(
        folder, configs, link, acq, stages, blob, branch_directory=branch
    ) as recorder:
        assert recorder.quality.step == recorder.step == 19
        recorder.advance(21)
        recorder.save_checkpoint()
    assert hashes(folder) == before
    bad = json.loads(blob)
    bad["quality"]["header"]["identity"]["run_id"] = "wrong"
    from zlscan.io.model_quality import digest

    bad["quality"].pop("checksum")
    bad["quality"]["checksum"] = digest(bad["quality"])
    bad.pop("checksum")
    bad["checksum"] = PhysicalRecorder._hash(bad)
    with pytest.raises(ValueError, match="identities"):
        PhysicalRecorder._restore_physics(configs, link, json.dumps(bad).encode())


def test_quality_merge_preserves_native_shapes_across_legacy_line_boundaries():
    cfg = legacy_dc_config(1.0)
    cfg.legacy_reset_per_line = True
    one = Chain(cfg)
    one.record_quality = True
    many = Chain(cfg)
    many.record_quality = True
    a = one.run_steps(10013, 10013)
    b = many.run_steps(10013, 997)
    assert a.quality_flags["front_end_clipped"].shape == (10013,)
    assert a.quality_flags["adc_saturated"].shape == a.codes.shape
    assert np.array_equal(a.codes, b.codes)
    assert all(np.array_equal(a.quality_flags[k], b.quality_flags[k]) for k in a.quality_flags)
