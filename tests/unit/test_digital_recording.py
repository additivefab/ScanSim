"""Resume existing raw and diagnostic files at a paired producer/file checkpoint."""

import json

import numpy as np
import pytest

from zlscan.adc.patterns import ConvertedSamples
from zlscan.adc_link.link import LinkConfig
from zlscan.contracts.time import CaptureClock, ClockDomain
from zlscan.digital.acquisition import AcquisitionConfig
from zlscan.engine.digital_pipeline import DigitalPipeline
from zlscan.io.rawstream import read_all
from zlscan.transport.queues import StageSpec

DOMAIN = ClockDomain(7, 125_000_003, phase_ticks=2**53 + 17)
ACQ = AcquisitionConfig(2, 7, sync_latency_cycles=2)
FIRST = 2**32 - 5


def setup(edge, stalled):
    link = LinkConfig(
        latency_half_cycles=25,
        propagation_ps=1500,
        capture_edge=edge,
        coding="twos_complement",
        channel_order=(1, 0),
        overflow_offset_cycles=-5,
    )
    clock = CaptureClock(DOMAIN, edge=edge)
    stages = [
        StageSpec("rx", 184, 2e9),
        StageSpec(
            "disk",
            92,
            1e9 if stalled else 1e12,
            stalls=((clock.tick_of(FIRST + 30), 500_000),) if stalled else (),
        ),
    ]
    pipeline = DigitalPipeline(DOMAIN, link, ACQ, stages, (0, 1))
    pipeline.schedule_syncs([clock.tick_of(FIRST + i) for i in (12, 30, 48, 66)])
    return pipeline, link, stages


def block(start, end):
    indices = np.arange(start, end)
    return ConvertedSamples(
        np.column_stack((indices + 100, indices + 1000)).astype(np.uint16),
        (0, 1),
        FIRST + start,
        4 if start < 25 else 5,
        DOMAIN,
        run_id="source",
        branch_id="branch",
        session_id=19,
    )


@pytest.mark.parametrize("edge", ["rising", "falling"])
@pytest.mark.parametrize("stalled", [False, True])
def test_existing_file_resume_matches_raw_and_journal_byte_for_byte(tmp_path, edge, stalled):
    from zlscan.engine.digital_recording import DigitalRecorder

    expected = tmp_path / "expected"
    actual = tmp_path / "actual"
    pipeline, link, stages = setup(edge, stalled)
    with DigitalRecorder(expected, pipeline, 19) as recorder:
        recorder.push(block(0, 25))
        recorder.push(block(25, 38))
        recorder.push(block(38, 73))
        recorder.finish()
        expected_summary = recorder.summary()
    pipeline, _, _ = setup(edge, stalled)
    with DigitalRecorder(actual, pipeline, 19) as recorder:
        recorder.push(block(0, 25))
        recorder.push(block(25, 38))
        saved = recorder.checkpoint()
        prefix = (actual / "raw.zlraw").read_bytes()
        saved_path = recorder.save_checkpoint()
        assert saved_path.read_bytes() == saved
    with DigitalRecorder.restore(actual, DOMAIN, link, ACQ, stages, (0, 1), saved) as recorder:
        assert recorder.next_source_index == FIRST + 38
        assert (actual / "raw.zlraw").read_bytes() == prefix
        recorder.push(block(38, 73))
        recorder.finish()
        assert recorder.summary() == expected_summary
        recorder.save_checkpoint()
    for name in ("raw.zlraw", "events.jsonl"):
        assert (actual / name).read_bytes() == (expected / name).read_bytes()
    assert not read_all(actual / "raw.zlraw").errors
    journal = [json.loads(line) for line in (actual / "events.jsonl").read_text().splitlines()]
    assert all(
        row["origin"] == "SIMULATED" and row["visibility"] == "ORACLE_ONLY" for row in journal
    )
    with pytest.raises(ValueError, match="tail"):
        DigitalRecorder.restore(actual, DOMAIN, link, ACQ, stages, (0, 1), saved)


def test_fault_between_raw_and_journal_keeps_prior_pair_recoverable(tmp_path, monkeypatch):
    from zlscan.engine.digital_recording import DigitalRecorder

    actual = tmp_path / "actual"
    pipeline, link, stages = setup("rising", False)
    with DigitalRecorder(actual, pipeline, 19) as recorder:
        recorder.push(block(0, 25))
        saved = recorder.checkpoint()
        append = recorder.journal.append

        def fail(data):
            append(data[: max(1, len(data) // 2)])
            raise OSError("injected journal failure")

        monkeypatch.setattr(recorder.journal, "append", fail)
        with pytest.raises(OSError, match="injected"):
            recorder.push(block(25, 38))
        with pytest.raises(OSError, match="failed"):
            recorder.checkpoint()
    raw_before = (actual / "raw.zlraw").read_bytes()
    journal_before = (actual / "events.jsonl").read_bytes()
    with pytest.raises(ValueError, match="tail"):
        DigitalRecorder.restore(actual, DOMAIN, link, ACQ, stages, (0, 1), saved)
    branch = tmp_path / "branch"
    with DigitalRecorder.restore(
        actual, DOMAIN, link, ACQ, stages, (0, 1), saved, branch_directory=branch
    ) as recorder:
        recorder.push(block(25, 38))
        recorder.push(block(38, 73))
        recorder.finish()
    assert (actual / "raw.zlraw").read_bytes() == raw_before
    assert (actual / "events.jsonl").read_bytes() == journal_before
    baseline = tmp_path / "baseline"
    pipeline, _, _ = setup("rising", False)
    with DigitalRecorder(baseline, pipeline, 19) as recorder:
        recorder.push(block(0, 25))
        recorder.push(block(25, 38))
        recorder.push(block(38, 73))
        recorder.finish()
    for name in ("raw.zlraw", "events.jsonl"):
        assert (baseline / name).read_bytes() == (branch / name).read_bytes()


def test_corrupted_journal_is_rejected_without_appending_raw(tmp_path):
    from zlscan.engine.digital_recording import DigitalRecorder

    root = tmp_path / "recording"
    pipeline, link, stages = setup("rising", False)
    with DigitalRecorder(root, pipeline, 19) as recorder:
        recorder.push(block(0, 25))
        recorder.push(block(25, 38))
        saved = recorder.checkpoint()
    path = root / "events.jsonl"
    data = bytearray(path.read_bytes())
    data[-3] ^= 1
    path.write_bytes(data)
    before = (root / "raw.zlraw").read_bytes()
    with pytest.raises(ValueError):
        DigitalRecorder.restore(root, DOMAIN, link, ACQ, stages, (0, 1), saved)
    assert (root / "raw.zlraw").read_bytes() == before


def test_observable_array_encoding_preserves_flags_and_large_counts():
    from zlscan.io.observable_arrays import decode_arrays, encode_arrays

    data = {
        "capture_time": np.array([2**53 + 17, 2**53 + 18], np.int64),
        "capture_count": np.array([2**64 - 2, 2**64 - 1], object),
        "overflow": np.array([True, False]),
        "overflow_valid": np.array([True, True]),
    }
    encoded = encode_arrays(data)
    decoded = decode_arrays(json.loads(json.dumps(encoded)))
    for key in data:
        np.testing.assert_array_equal(data[key], decoded[key])
    normal = {"window_count": np.array([2**32, 2**32 + 1], np.int64)}
    object_count = {"window_count": np.array([2**32, 2**32 + 1], object)}
    assert encode_arrays(normal) == encode_arrays(object_count)


def test_checkpoint_waits_for_whole_producer_and_file_operation(tmp_path, monkeypatch):
    import concurrent.futures
    import threading

    from zlscan.engine.digital_recording import DigitalRecorder

    pipeline, _, _ = setup("rising", False)
    with DigitalRecorder(tmp_path / "recording", pipeline, 19) as recorder:
        recorder.push(block(0, 25))
        entered = threading.Event()
        release = threading.Event()
        started = threading.Event()
        original = recorder.writer.write_block

        def paused(**kwargs):
            entered.set()
            assert release.wait(5)
            return original(**kwargs)

        monkeypatch.setattr(recorder.writer, "write_block", paused)

        def save():
            started.set()
            return recorder.checkpoint()

        with concurrent.futures.ThreadPoolExecutor(2) as workers:
            producer = workers.submit(recorder.push, block(25, 38))
            try:
                assert entered.wait(5)
                pending = workers.submit(save)
                assert started.wait(5)
                assert not pending.done()
            finally:
                release.set()
            producer.result(5)
            saved = pending.result(5)
        value = json.loads(saved)
        assert value["writer"]["next_sequence"] == "1"
        assert value["pipeline"]["state"]["link"]["state"]["next_index"] == str(FIRST + 38)


def test_journal_record_count_is_checked_against_actual_prefix(tmp_path):
    import hashlib

    from zlscan.engine.digital_recording import DigitalRecorder

    root = tmp_path / "recording"
    pipeline, link, stages = setup("rising", False)
    with DigitalRecorder(root, pipeline, 19) as recorder:
        recorder.push(block(0, 25))
        value = json.loads(recorder.checkpoint())
    value["records"] = "99"
    value.pop("checksum")
    value["checksum"] = hashlib.sha256(
        json.dumps(value, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()
    before = {name: (root / name).read_bytes() for name in ("raw.zlraw", "events.jsonl")}
    with pytest.raises(ValueError, match="record count"):
        DigitalRecorder.restore(root, DOMAIN, link, ACQ, stages, (0, 1), json.dumps(value).encode())
    assert before == {name: (root / name).read_bytes() for name in before}
