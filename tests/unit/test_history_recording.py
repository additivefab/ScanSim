"""Producer operations bind source batches and consumed digital state to one recovery root."""

import json
from dataclasses import replace

import pytest
from tests.unit.test_physical_recording import settings

from zlscan.engine.physical_recording import PhysicalRecorder
from zlscan.io.producer_history import HistoryReader


def create(path, **kwargs):
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
        **kwargs,
    )


@pytest.mark.parametrize("quality", [False, True])
def test_original_operations_resume_and_do_not_change_adc(tmp_path, quality):
    def first(r):
        r.schedule_syncs([r.domain.tick_of(i) for i in (20, 50)])
        r.advance(57)
        r.request(0, "detector.gain", 2400, effective_step=113)
        r.advance(80)

    def last(r):
        r.advance(160)
        r.finish()
        r.save_checkpoint()

    with create(tmp_path / "plain") as r:
        first(r)
        last(r)
        expected = {c: chain.checkpoint() for c, chain in r.chains.items()}
    with create(tmp_path / "logged", record_digital_history=True, record_quality=quality) as r:
        first(r)
        root = r.save_checkpoint().read_bytes()
        assert json.loads(root)["format"] == PhysicalRecorder.HISTORY_FORMAT
    with PhysicalRecorder.restore(tmp_path / "logged", *settings(), root) as r:
        last(r)
        assert {c: chain.checkpoint() for c, chain in r.chains.items()} == expected
        value = json.loads(r.checkpoint())
        reader = HistoryReader(r.directory / "producer_history.jsonl", value["digital_history"])
        rows = list(reader.after(None))
        assert [row["op"] for row in rows] == [
            "sync",
            "advance",
            "advance",
            "advance",
            "advance",
            "finish",
        ]
        assert rows[-1]["after"] == r._hash_bytes(r.recording.pipeline.checkpoint())
    assert (tmp_path / "logged/raw.zlraw").read_bytes() == (
        tmp_path / "plain/raw.zlraw"
    ).read_bytes()


def test_unlogged_pipeline_mutation_cannot_publish(tmp_path):
    with create(tmp_path / "mutation", record_digital_history=True) as r:
        before = r.save_checkpoint().read_bytes()
        r.recording.schedule_syncs([r.domain.tick_of(100)])
        with pytest.raises(ValueError, match="history"):
            r.save_checkpoint()
        assert (r.directory / "checkpoint.json").read_bytes() == before


def test_unknown_history_tail_requires_branch_and_preserves_original(tmp_path):
    folder = tmp_path / "run"
    with create(folder, record_digital_history=True) as r:
        r.advance(19)
        root = r.save_checkpoint().read_bytes()
        r.advance(13)
    original = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
    with pytest.raises(ValueError, match="tail"):
        PhysicalRecorder.restore(folder, *settings(), root)
    with PhysicalRecorder.restore(
        folder, *settings(), root, branch_directory=tmp_path / "branch"
    ) as r:
        r.advance(13)
        r.finish()
        r.save_checkpoint()
        rows = list(
            HistoryReader(
                r.directory / "producer_history.jsonl",
                json.loads(r.checkpoint())["digital_history"],
            ).after(None)
        )
        assert [row["op"] for row in rows] == ["advance", "advance", "finish"]
    assert {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()} == original


def test_failed_append_cannot_publish(tmp_path, monkeypatch):
    with create(tmp_path / "failed", record_digital_history=True) as r:
        before = r.save_checkpoint().read_bytes()

        def broken(*args):
            raise OSError("producer history storage failed")

        monkeypatch.setattr(r.digital_history, "advance", broken)
        with pytest.raises(OSError, match="storage failed"):
            r.advance(13)
        with pytest.raises(OSError, match="failed"):
            r.save_checkpoint()
        assert (r.directory / "checkpoint.json").read_bytes() == before


def test_public_input_enables_history_and_replay_checks_its_committed_prefix(tmp_path, monkeypatch):
    from tests.unit.test_recording_workbench import source

    from zlscan.engine.recording_replay import RecordingReplay

    folder = source(tmp_path, monkeypatch, record_digital_history=True)
    with RecordingReplay(folder) as replay:
        assert replay.history_reader is not None
        assert list(replay.history_reader.after(None))[-1]["op"] == "finish"
    file = folder / "producer_history.jsonl"
    body = bytearray(file.read_bytes())
    body[-10] ^= 1
    file.write_bytes(body)
    with pytest.raises(ValueError, match="SHA|hash|prefix"):
        RecordingReplay(folder)


def test_original_grouping_reproduces_every_consumed_pipeline_hash(tmp_path):
    import hashlib

    import numpy as np

    from zlscan.adc.patterns import ConvertedSamples
    from zlscan.engine.chain import Chain
    from zlscan.engine.digital_pipeline import DigitalPipeline

    with create(tmp_path / "run", record_digital_history=True) as r:
        r.schedule_syncs([r.domain.tick_of(i) for i in (20, 50, 80, 110)])
        r.advance(57)
        r.request(0, "detector.gain", 2400, effective_step=113)
        r.advance(280)
        r.reset_control()
        r.start()
        r.advance(450)
        r.finish()
        root = json.loads(r.checkpoint())
        rows = list(
            HistoryReader(r.directory / "producer_history.jsonl", root["digital_history"]).after(
                None
            )
        )
        configs, link, acq, stages = settings()
        pipeline = DigitalPipeline(r.domain, link, acq, stages, r.channels)
        chains = {c: Chain(cfg) for c, cfg in configs.items()}
        for row in rows:
            if row["op"] == "sync":
                pipeline.schedule_syncs([int(t) for t in row["ticks"]])
                pipeline.drain()
            elif row["op"] == "finish":
                pipeline.finish()
                pipeline.drain()
            else:
                step, end = int(row["step"]), int(row["end"])
                if step == 113:
                    chains[0].cfg.detector = replace(chains[0].cfg.detector, gain=2400)
                outputs = [chains[c].advance(end - step) for c in r.channels]
                codes = np.column_stack([out.codes for out in outputs])
                first = outputs[0].first_sample
                for part in row["sources"]:
                    begin, count = int(part["first_index"]), part["count"]
                    words = codes[begin - first : begin - first + count]
                    assert (
                        hashlib.sha256(
                            np.ascontiguousarray(words, dtype="<u2").tobytes()
                        ).hexdigest()
                        == part["codes_sha256"]
                    )
                    pipeline.push(
                        ConvertedSamples(
                            words,
                            r.channels,
                            begin,
                            part["epoch"],
                            r.domain,
                            link.bits,
                            **r.identity,
                        )
                    )
                    pipeline.drain()
            assert hashlib.sha256(pipeline.checkpoint()).hexdigest() == row["after"]
