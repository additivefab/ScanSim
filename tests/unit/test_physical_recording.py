"""One recovery root binds physical state, scheduled control, digital payloads and file cursors."""

import json
from copy import deepcopy
from dataclasses import replace

import numpy as np
import pytest

from zlscan.adc_link.link import LinkConfig
from zlscan.config.presets import legacy_dc_config
from zlscan.control.state import State
from zlscan.digital.acquisition import AcquisitionConfig
from zlscan.engine.chain import Chain
from zlscan.io.rawstream import read_all
from zlscan.transport.queues import StageSpec


def settings(edge="rising", stalled=False):
    a = legacy_dc_config(1.0)
    a.detector = replace(a.detector, pole_hz=70_000, shot_noise=True)
    a.afe = replace(a.afe, pole_hz=50_000, aa_poles_hz=(90_000,))
    b = deepcopy(a)
    b.seed = 31
    configs = {0: a, 1: b}
    link = LinkConfig(
        latency_half_cycles=25,
        propagation_ps=1500,
        capture_edge=edge,
        channel_order=(1, 0),
        coding="twos_complement",
    )
    acquisition = AcquisitionConfig(2, 13)
    stages = [
        StageSpec("rx", 232, 2e9),
        StageSpec(
            "disk",
            116,
            1e8 if stalled else 1e12,
            stalls=((40_000_000, 500_000_000),) if stalled else (),
        ),
    ]
    return configs, link, acquisition, stages


def create(path, edge="rising", stalled=False):
    from zlscan.engine.physical_recording import PhysicalRecorder

    configs, link, acq, stages = settings(edge, stalled)
    recorder = PhysicalRecorder(
        path, configs, link, acq, stages, session_id=19, run_id="physical", branch_id="main"
    )
    recorder.schedule_syncs([recorder.domain.tick_of(i) for i in (20, 50, 80, 110)])
    return recorder


def prefix(recorder):
    recorder.advance(57)
    recorder.request(
        0, "detector.gain", 2400, effective_step=113, readback_step=351, readback_value=2400
    )
    recorder.request(1, "source.power_w", 4e-3, effective_step=381)
    recorder.advance(280)
    assert recorder.step == 337


def continuation(recorder):
    recorder.advance(103)
    assert recorder.step == 440
    recorder.reset_control()
    assert recorder.controller.state is State.UNCONFIGURED
    recorder.start()
    recorder.advance(347)
    recorder.finish()


@pytest.mark.parametrize("edge", ["rising", "falling"])
@pytest.mark.parametrize("stalled", [False, True])
def test_combined_resume_preserves_actual_files_physics_and_controls(tmp_path, edge, stalled):
    from zlscan.engine.physical_recording import PhysicalRecorder

    with create(tmp_path / "baseline", edge, stalled) as recorder:
        prefix(recorder)
        continuation(recorder)
        expected = recorder.checkpoint()
        expected_summary = recorder.summary()
    with create(tmp_path / "actual", edge, stalled) as recorder:
        prefix(recorder)
        assert recorder.applied_commands == 1
        assert recorder.recording.pipeline.link.pending_count > 0
        blob = recorder.checkpoint()
        assert recorder.save_checkpoint().read_bytes() == blob
    configs, link, acq, stages = settings(edge, stalled)
    with PhysicalRecorder.restore(
        tmp_path / "actual", configs, link, acq, stages, blob
    ) as recorder:
        assert recorder.step == 337 and recorder.applied_commands == 1
        assert not recorder.controller.readback_confirmed(
            "channel.0.detector.gain", at_tick=recorder.fine_clock.tick_of(recorder.step)
        )
        continuation(recorder)
        assert recorder.checkpoint() == expected
        assert recorder.summary() == expected_summary
    for name in ("raw.zlraw", "events.jsonl"):
        assert (tmp_path / "actual" / name).read_bytes() == (
            tmp_path / "baseline" / name
        ).read_bytes()


def test_control_changes_reach_model_and_raw_samples_not_just_epoch_labels(tmp_path):
    with create(tmp_path / "run") as recorder:
        prefix(recorder)
        recorder.advance(450)
        recorder.finish()
        assert recorder.chains[0].cfg.detector.gain == 2400
        assert recorder.chains[1].cfg.source.power_w == 4e-3
        actual = read_all(tmp_path / "run/raw.zlraw")
        assert actual.segments and not actual.errors
        configs, _, _, _ = settings()
        expected = []
        for channel, config in configs.items():
            chain = Chain(config)
            parts = [chain.advance(113).codes]
            if channel == 0:
                chain.cfg.detector = replace(chain.cfg.detector, gain=2400)
            parts.append(chain.advance(268).codes)
            if channel == 1:
                chain.cfg.source = replace(chain.cfg.source, power_w=4e-3)
            parts.append(chain.advance(406).codes)
            expected.append(np.concatenate(parts))
            assert json.loads(chain.checkpoint()) == json.loads(
                recorder.chains[channel].checkpoint()
            )
        expected = np.column_stack(expected)
        unchanged = np.column_stack(
            [Chain(config).advance(787).codes for config in settings()[0].values()]
        )
        assert np.any(expected != unchanged)
        for segment in actual.segments:
            start = segment.first_sample_index
            np.testing.assert_array_equal(
                segment.samples, expected[start : start + len(segment.samples)]
            )


def test_rejections_do_not_advance_physics_or_commands(tmp_path):
    with create(tmp_path / "run") as recorder:
        prefix(recorder)
        before = recorder.checkpoint()
        actions = [
            lambda: recorder.advance(0),
            lambda: recorder.advance(1.5),
            lambda: recorder.request(9, "detector.gain", 3, effective_step=200),
            lambda: recorder.request(0, "unknown", 3, effective_step=200),
            lambda: recorder.request(0, "detector.gain", -1, effective_step=200),
            lambda: recorder.request(0, "source.power_w", float("inf"), effective_step=200),
            lambda: recorder.request(0, "detector.gain", 3, effective_step=336),
            recorder.reset_control,
            recorder.finish,
        ]
        for action in actions:
            with pytest.raises(ValueError):
                action()
            assert recorder.checkpoint() == before


def test_corrupt_or_cross_boundary_checkpoint_rejected_without_output_mutation(tmp_path):
    from zlscan.engine.physical_recording import PhysicalRecorder

    directory = tmp_path / "run"
    with create(directory) as recorder:
        prefix(recorder)
        blob = recorder.checkpoint()
    configs, link, acq, stages = settings()
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    for mutate in (
        lambda d: d["chains"]["0"].update(step="336"),
        lambda d: d.update(applied_commands="0"),
        lambda d: d["commands"][1].update(value=5e-3),
        lambda d: d["identity"].update(run_id="other"),
    ):
        value = json.loads(blob)
        mutate(value)
        value.pop("checksum")
        value["checksum"] = PhysicalRecorder._hash(value)
        with pytest.raises(ValueError):
            PhysicalRecorder.restore(
                directory, configs, link, acq, stages, json.dumps(value).encode()
            )
        assert before == {p.name: p.read_bytes() for p in directory.iterdir()}


def test_unknown_tail_requires_preserved_branch_with_full_composite_restore(tmp_path):
    from zlscan.engine.physical_recording import PhysicalRecorder

    directory = tmp_path / "run"
    with create(directory) as recorder:
        prefix(recorder)
        saved = recorder.checkpoint()
        continuation(recorder)
    before = {p.name: p.read_bytes() for p in directory.iterdir()}
    configs, link, acq, stages = settings()
    with pytest.raises(ValueError, match="tail"):
        PhysicalRecorder.restore(directory, configs, link, acq, stages, saved)
    branch = tmp_path / "recovered"
    with PhysicalRecorder.restore(
        directory, configs, link, acq, stages, saved, branch_directory=branch
    ) as recorder:
        continuation(recorder)
    assert before == {p.name: p.read_bytes() for p in directory.iterdir()}
    for name in ("raw.zlraw", "events.jsonl"):
        assert (branch / name).read_bytes() == (directory / name).read_bytes()


@pytest.mark.parametrize("pause", [0, 1, 3, 4, 5])
def test_non_sample_fine_boundaries_restore_and_command_takes_effect_before_sample(tmp_path, pause):
    from zlscan.engine.physical_recording import PhysicalRecorder

    configs, link, acq, stages = settings()
    with create(tmp_path / "baseline") as baseline:
        baseline.request(0, "detector.gain", 2400, effective_step=4)
        if pause:
            baseline.advance(pause)
        blob = baseline.checkpoint()
        baseline.advance(120)
        baseline.finish()
        expected = baseline.checkpoint()
    with PhysicalRecorder.restore(
        tmp_path / "baseline",
        configs,
        link,
        acq,
        stages,
        blob,
        branch_directory=tmp_path / "resumed",
    ) as resumed:
        resumed.advance(120)
        resumed.finish()
        assert resumed.checkpoint() == expected


def test_partial_physical_failure_cannot_publish_a_new_root(tmp_path, monkeypatch):
    from zlscan.engine.physical_recording import PhysicalRecorder

    directory = tmp_path / "run"
    with create(directory) as recorder:
        prefix(recorder)
        saved = recorder.save_checkpoint().read_bytes()

        def fail(_):
            raise OSError("injected channel failure")

        monkeypatch.setattr(recorder.chains[1], "advance", fail)
        with pytest.raises(OSError, match="injected"):
            recorder.advance(50)
        assert recorder.chains[0].step != recorder.chains[1].step
        with pytest.raises(OSError, match="failed"):
            recorder.save_checkpoint()
        assert (directory / "checkpoint.json").read_bytes() == saved
    configs, link, acq, stages = settings()
    with PhysicalRecorder.restore(directory, configs, link, acq, stages, saved) as recorder:
        continuation(recorder)
        assert recorder.controller.state is State.DONE


def test_incremental_unknown_length_schedule_does_not_keep_all_past_windows(tmp_path):
    from zlscan.engine.physical_recording import PhysicalRecorder

    configs, link, acq, stages = settings()
    for cfg in configs.values():
        cfg.detector = replace(cfg.detector, shot_noise=False)
    sizes = []
    with PhysicalRecorder(
        tmp_path / "online",
        configs,
        link,
        acq,
        stages,
        session_id=19,
        run_id="online",
        branch_id="main",
    ) as recorder:
        for i in range(600):
            recorder.schedule_syncs([recorder.domain.tick_of(32 * i + 20)])
            recorder.advance(128)
            if i in (99, 299, 599):
                sizes.append(len(recorder.checkpoint()))
        recorder.finish()
        assert recorder.summary()["written_blocks"] == 600
        assert max(sizes) < 40_000
        assert max(sizes) - min(sizes) < 2048


def test_global_checkpoint_waits_for_both_analog_channels_and_digital_write(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    ready, release = Event(), Event()
    with create(tmp_path / "run") as recorder:
        original = recorder.chains[1].advance

        def blocked(count):
            ready.set()
            assert release.wait(5)
            return original(count)

        monkeypatch.setattr(recorder.chains[1], "advance", blocked)
        with ThreadPoolExecutor(max_workers=2) as pool:
            producer = pool.submit(recorder.advance, 137)
            assert ready.wait(5)
            snapshot_started = Event()

            def snapshot():
                snapshot_started.set()
                return recorder.checkpoint()

            checkpoint = pool.submit(snapshot)
            assert snapshot_started.wait(5)
            assert not checkpoint.done()
            release.set()
            producer.result(timeout=5)
            value = json.loads(checkpoint.result(timeout=5))
        assert {v["step"] for v in value["chains"].values()} == {"137"}
        assert value["recording"]["pipeline"]["state"]["link"]["state"]["next_index"] == "35"
