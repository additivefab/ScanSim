"""Producer export follows delivered source epochs, including logical reset and recovery."""

import pytest
from tests.unit.test_physical_recording import continuation, create, prefix, settings

from zlscan.contracts.calibration import CaptureDescription
from zlscan.engine.physical_recording import PhysicalRecorder
from zlscan.io.manifest import sha256_file
from zlscan.io.rawstream import scan_blocks


def test_export_only_delivered_epochs_and_effective_settings(tmp_path):
    with create(tmp_path / "run") as recorder:
        assert recorder.capture_description() is None
        prefix(recorder)
        partial = recorder.capture_description()
        assert partial.version == 2
        assert partial.raw_sha256 == sha256_file(recorder.directory / "raw.zlraw")
        checkpoint = recorder.checkpoint()
        assert checkpoint == recorder.checkpoint()  # export is observational
        continuation(recorder)
        capture = recorder.capture_description()
        raw = scan_blocks(recorder.directory / "raw.zlraw", lambda *_: None)
        assert {e.epoch for e in capture.epoch_bindings} == {b.epoch for b in raw.blocks}
        assert capture.lines == 1 + max(b.line_number for b in raw.blocks)
        assert capture.binding.simulation_hash is None
        assert capture.binding.channel_roles == raw.session.channel_roles
        assert all(e.binding.hardware_hash is None for e in capture.epoch_bindings)
        # A logical reset clears the controller's settings dictionary, not the physical gain.
        bindings = {e.epoch: e.binding.simulation_hash for e in capture.epoch_bindings}
        assert bindings[0] != bindings[1]
        assert bindings[2] == bindings[3]
        assert CaptureDescription.model_validate_json(capture.model_dump_json()) == capture


def test_export_resume_and_branch_match_without_mutating_source(tmp_path):
    with create(tmp_path / "source") as recorder:
        prefix(recorder)
        checkpoint = recorder.checkpoint()
        continuation(recorder)
        expected = recorder.capture_description()
    source_bytes = (tmp_path / "source" / "raw.zlraw").read_bytes()
    configs, link, acquisition, stages = settings()
    with PhysicalRecorder.restore(
        tmp_path / "source",
        configs,
        link,
        acquisition,
        stages,
        checkpoint,
        branch_directory=tmp_path / "branch",
    ) as recorder:
        continuation(recorder)
        assert recorder.capture_description() == expected
    assert (tmp_path / "source" / "raw.zlraw").read_bytes() == source_bytes


def test_export_rejects_external_tail(tmp_path):
    with create(tmp_path / "run") as recorder:
        prefix(recorder)
        with (recorder.directory / "raw.zlraw").open("ab") as stream:
            stream.write(b"uncommitted")
        with pytest.raises((ValueError, OSError)):
            recorder.capture_description()


def test_future_request_is_not_an_observed_binding(tmp_path):
    with create(tmp_path / "run") as recorder:
        recorder.advance(200)
        before = recorder.capture_description()
        recorder.request(0, "detector.gain", 8000, effective_step=2000)
        assert recorder.capture_description() == before


def test_single_channel_identity_matches_static_scans(tmp_path):
    from dataclasses import replace

    from zlscan.engine.physical_run import simulation_binding

    configs, link, acq, stages = settings()
    link = replace(link, channel_order=(0,))
    with PhysicalRecorder(
        tmp_path / "single",
        {0: configs[0]},
        link,
        acq,
        stages,
        session_id=19,
        run_id="single",
        branch_id="main",
    ) as recorder:
        recorder.schedule_syncs([recorder.domain.tick_of(20), recorder.domain.tick_of(80)])
        recorder.request(0, "detector.gain", 2400, effective_step=113)
        recorder.advance(440)
        recorder.finish()
        capture = recorder.capture_description()
        assert (
            capture.binding_for_epoch(0).simulation_hash
            == simulation_binding(configs[0]).simulation_hash
        )
        changed = configs[0]
        recorder._apply_config(changed, {"parameter": "detector.gain", "value": 2400})
        assert (
            capture.binding_for_epoch(1).simulation_hash
            == simulation_binding(changed).simulation_hash
        )


def test_export_feeds_existing_reconstruction_and_missing_epoch_refuses(tmp_path):
    # Deliberately artificial transfer table: this checks software wiring, not physical accuracy.
    from tests.unit.test_density_reconstruction import calibration

    from zlscan.contracts.calibration import CalibrationSet
    from zlscan.recon.capture import reconstruct_capture
    from zlscan.recon.density import Grid

    with create(tmp_path / "run") as recorder:
        prefix(recorder)
        continuation(recorder)
        capture = recorder.capture_description()
    headers = []
    for entry in capture.epoch_bindings:
        header = calibration()
        header.calibration_id = f"synthetic-table-{entry.epoch}"
        header.binds_to = entry.binding
        header.items["transfer_lut"].value["channels"]["0"]["code_knots"] = [0, 20000, 40000, 65535]
        header.items["dark_raw_code"].value["0"]["median"] = [0] * 13
        header.items["white_raw_code"].value["0"]["median"] = [40000] * 13
        headers.append(header)
    collection = CalibrationSet(calibration_set_id="wiring-only", calibrations=headers)
    result = reconstruct_capture(
        tmp_path / "run" / "raw.zlraw",
        capture,
        collection,
        Grid(13, capture.lines, 1, 1),
        tmp_path / "image",
    )
    assert result["epoch_calibrations"] == {
        str(e.epoch): f"synthetic-table-{e.epoch}" for e in capture.epoch_bindings
    }
    collection.calibrations.pop()
    with pytest.raises(ValueError):
        reconstruct_capture(
            tmp_path / "run" / "raw.zlraw",
            capture,
            collection,
            Grid(13, capture.lines, 1, 1),
            tmp_path / "rejected",
        )
    assert not (tmp_path / "rejected").exists()
