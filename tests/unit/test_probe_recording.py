"""Probe files participate in the same physical recovery root without perturbing data."""

import json

import pytest
from tests.unit.test_physical_recording import settings

from zlscan.engine.physical_recording import PhysicalRecorder


def traced(path, raw=False):
    from zlscan.io.probe_store import TracePolicy

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
        trace_policy=TracePolicy(probes=("power_w", "v_adc"), base=8, factor=4, keep_raw=raw),
    )


def test_probe_recording_and_resume_do_not_change_adc_or_physical_state(tmp_path):
    from zlscan.io.probe_store import TraceReader

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
    ) as plain:
        plain.schedule_syncs([plain.domain.tick_of(20)])
        plain.advance(231)
        plain.finish()
        plain.save_checkpoint()
        expected = {c: chain.checkpoint() for c, chain in plain.chains.items()}
    folder = tmp_path / "trace"
    with traced(folder, True) as recorder:
        recorder.schedule_syncs([recorder.domain.tick_of(20)])
        recorder.advance(19)
        recorder.save_checkpoint()
    with PhysicalRecorder.restore(
        folder, configs, link, acq, stages, (folder / "checkpoint.json").read_bytes()
    ) as recorder:
        recorder.advance(212)
        recorder.finish()
        recorder.save_checkpoint()
        assert {c: chain.checkpoint() for c, chain in recorder.chains.items()} == expected
        root = json.loads((folder / "checkpoint.json").read_bytes())
    assert (folder / "raw.zlraw").read_bytes() == (tmp_path / "plain/raw.zlraw").read_bytes()
    with TraceReader(folder / "probes", root["trace"]) as reader:
        assert len(reader.query(1, "power_w", 0, 231, 200)["values"]) == 231


def test_failed_probe_append_cannot_publish_advanced_root(tmp_path, monkeypatch):
    with traced(tmp_path / "run") as recorder:
        recorder.advance(19)
        root = recorder.save_checkpoint().read_bytes()

        def broken(*args):
            raise OSError("probe disk full")

        monkeypatch.setattr(recorder.trace, "append", broken)
        with pytest.raises(OSError, match="disk full"):
            recorder.advance(17)
        with pytest.raises(OSError, match="failed"):
            recorder.save_checkpoint()
        assert (recorder.directory / "checkpoint.json").read_bytes() == root


def test_control_change_and_reset_keep_probe_time_continuous(tmp_path):
    from zlscan.io.probe_store import TraceReader

    with traced(tmp_path / "run", True) as recorder:
        recorder.request(0, "source.power_w", 0.002, effective_step=13)
        recorder.advance(17)
        recorder.reset_control()
        recorder.start()
        recorder.advance(12)
        root = json.loads(recorder.checkpoint())
    with TraceReader(tmp_path / "run/probes", root["trace"]) as reader:
        trace = reader.query(0, "power_w", 0, 29, 100)
        assert len(trace["values"]) == 29
        assert trace["values"][12] != trace["values"][13]


def test_public_cli_persisted_queries_resume_and_branch_preserve_source(tmp_path, monkeypatch):
    from tests.unit.test_recording_workbench import hashes, source

    from zlscan.cli import main
    from zlscan.engine.recording_replay import RecordingReplay
    from zlscan.engine.saved_service import saved_service

    folder = source(
        tmp_path,
        monkeypatch,
        trace=dict(probes=["power_w", "v_adc"], base=8, factor=4, keep_raw=True),
    )
    before = hashes(folder)
    root = json.loads((folder / "checkpoint.json").read_bytes())
    assert root["format"] == PhysicalRecorder.TRACE_FORMAT
    assert int(root["trace"]["step"]) == 11000
    with saved_service(folder) as service, RecordingReplay(folder) as model:
        expected = model.replay(10200, 800, {"power_w"})
        for channel in [0, 1]:
            run = service.runs[f"recording-c{channel}"]
            actual = service.traces(run, "pmt.power_w", 10200, 11000, 800, "research")
            assert actual["derivation"] == "recorded"
            assert actual["values"] == expected["power_w"][:, channel].tolist()
            assert (
                service.traces(run, "pmt.power_w", 10200, 11000, 800, "instrument")["status"]
                == "not_recorded"
            )
            assert set(service.manifest(run)["recorded_probes"]) == {"power_w", "v_adc"}
    assert before == hashes(folder)
    # A branch from a completed recording can query/commit but cannot restart physics.
    command_file = tmp_path / "commands.jsonl"
    command_file.write_text("")
    branch = tmp_path / "branch"
    assert (
        main(
            [
                "run",
                "--mode",
                "recording",
                "--resume",
                str(folder),
                "--out",
                str(branch),
                "--commands",
                str(command_file),
            ]
        )
        == 0
    )
    assert hashes(folder) == before
    with saved_service(branch) as service:
        assert (
            service.traces(
                service.runs["recording-c1"], "pmt.power_w", 10200, 11000, 800, "research"
            )["values"]
            == expected["power_w"][:, 1].tolist()
        )


def test_summary_queries_use_persisted_data_and_fine_view_replays(tmp_path, monkeypatch):
    from tests.unit.test_recording_workbench import source

    from zlscan.engine.saved_service import saved_service

    folder = source(tmp_path, monkeypatch, trace=dict(probes=["power_w"], base=8, factor=4))
    with saved_service(folder) as service:
        run = service.runs["recording-c0"]
        coarse = service.traces(run, "pmt.power_w", 0, 11000, 40, "research")
        assert coarse["derivation"] == "recorded" and len(coarse["values"]) <= 40
        fine = service.traces(run, "pmt.power_w", 10200, 10210, 40, "research")
        assert fine["derivation"] == "replayed" and len(fine["values"]) == 10


def test_uncommitted_probe_progress_branches_from_old_root_without_truncation(tmp_path):
    from tests.unit.test_recording_workbench import hashes

    from zlscan.io.probe_store import TraceReader

    folder = tmp_path / "run"
    configs, link, acq, stages = settings()
    with traced(folder, True) as recorder:
        recorder.advance(19)
        root = recorder.save_checkpoint().read_bytes()
        recorder.advance(17)  # Process exits without publishing this progress.
    before = hashes(folder)
    with pytest.raises(ValueError, match="tail|orphan"):
        PhysicalRecorder.restore(folder, configs, link, acq, stages, root)
    branch = tmp_path / "branch"
    with PhysicalRecorder.restore(
        folder, configs, link, acq, stages, root, branch_directory=branch
    ) as recorder:
        assert recorder.step == 19 and recorder.trace.step == 19
        recorder.advance(21)
        checkpoint = json.loads(recorder.checkpoint())
    with TraceReader(branch / "probes", checkpoint["trace"]) as reader:
        assert len(reader.query(0, "power_w", 0, 40, 100)["values"]) == 40
    assert hashes(folder) == before
