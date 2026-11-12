"""Saved recording workbench keeps delivered samples separate from model replay."""

import hashlib
import io
import json
import sys

import numpy as np
import pytest

from zlscan.cli import main
from zlscan.engine.saved_service import saved_service
from zlscan.io.rawstream import read_all


def hashes(path):
    return {
        p.relative_to(path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in path.rglob("*")
        if p.is_file()
    }


def source(tmp_path, monkeypatch, **changes):
    config = {
        "format": "zlscan.physical-recording/1",
        "model_version": "chain-0.3",
        "preset": "nominal",
        "chain": {
            "seed": 3,
            "source": {"gated": False},
            "detector": {"shot_noise": False},
            "psf": {"nodes_x": 1, "nodes_y": 1},
        },
        "secondary": {"seed": 31},
        "link": {"latency_half_cycles": 25, "propagation_ps": 1500},
        "acquisition": {"window_start": 90, "window_length": 100},
        "stages": [{"name": "disk", "capacity_bytes": 4096, "rate_Bps": 1e9}],
        "session_id": 19,
        "run_id": "physical",
        "branch_id": "main",
        "chunk_steps": 97,
    }
    config.update(changes)
    p = tmp_path / "config.json"
    p.write_text(json.dumps(config))
    commands = [
        {"op": "lines", "count": 1},
        {
            "op": "request",
            "channel": 0,
            "parameter": "source.power_w",
            "value": 0.0002,
            "effective_step": 10321,
        },
        {"op": "sync", "ticks": ["2500007000"]},
        {"op": "advance", "steps": 450},
        {"op": "reset"},
        {"op": "start"},
        {"op": "advance", "steps": 550},
        {"op": "finish"},
    ]
    monkeypatch.setattr(sys, "stdin", io.StringIO("".join(json.dumps(c) + "\n" for c in commands)))
    out = tmp_path / "run"
    assert main(["run", "--mode", "recording", "--config", str(p), "--out", str(out)]) == 0
    return out


def test_registration_is_read_only_and_does_not_advance_or_open_producer(tmp_path, monkeypatch):
    from zlscan.engine.chain import Chain
    from zlscan.engine.digital_recording import DigitalRecorder

    out = source(tmp_path, monkeypatch)
    before = hashes(out)

    def forbidden(*a, **k):
        raise AssertionError("registration is read only")

    monkeypatch.setattr(Chain, "advance", forbidden)
    monkeypatch.setattr(DigitalRecorder, "restore", forbidden)
    monkeypatch.setattr(DigitalRecorder, "__init__", forbidden)
    with saved_service(out) as service:
        assert set(service.runs) == {"recording-c0", "recording-c1"}
        for run in service.runs.values():
            m = service.manifest(run)
            assert m["read_only"] and m["loaded_from_saved"] and m["frozen_commit"]
            assert m["steps"] == 11000 and not m["live_follow"]
            t = service.traces(run, "adc.code", 0, 11000, 40, "instrument")
            assert len(t["values"]) <= 40 and t["visibility"] == "OBSERVABLE"
            assert service.replay(run, 10200, 100, [], "instrument")["status"] == "not_computed"
            assert service.state(run, 10200, {"power_w"}, "instrument")["status"] == "not_recorded"
    assert hashes(out) == before


def test_each_channel_query_matches_delivered_samples_and_original_epochs(tmp_path, monkeypatch):
    out = source(tmp_path, monkeypatch)
    data = read_all(out / "raw.zlraw")
    with saved_service(out) as service:
        for channel in [0, 1]:
            run = service.runs[f"recording-c{channel}"]
            t = service.traces(run, "adc.code", 10200, 11000, 400, "instrument")
            expected = [None] * 200
            epochs = [None] * 200
            for seg in data.segments:
                for index, row in enumerate(seg.samples):
                    pos = seg.first_sample_index + index - 2550
                    if 0 <= pos < 200:
                        expected[pos] = int(row[channel])
                        epochs[pos] = seg.epoch
            assert t["values"] == expected and t["epochs"] == epochs
            assert t["missing_count"] == expected.count(None)
            assert sum(t["recorded_counts"]) == 200 - expected.count(None)
            coarse = service.traces(run, "adc.code", 10200, 11000, 1, "instrument")
            assert coarse["mixed_epoch"] == [True]
            assert coarse["epochs"] == [None]
            assert coarse["recorded_counts"] == [100]


def test_model_replay_changes_parameters_at_original_steps_without_filling_delivery(
    tmp_path, monkeypatch
):
    from zlscan.engine.recording_replay import RecordingReplay

    out = source(tmp_path, monkeypatch)
    before = hashes(out)
    with saved_service(out) as service, RecordingReplay(out) as model:
        run = service.runs["recording-c0"]
        result = service.replay(run, 10200, 800, ["power_w"])
        expected = model.replay(10200, 800, {"power_w"})
        assert result["visibility"] == "ORACLE_ONLY"
        assert result["codes"] == expected["codes"][:, 0].tolist()
        assert result["probes"]["pmt.power_w"] == expected["power_w"][:, 0].tolist()
        state = service.state(run, 10321, {"power_w"})
        assert state["status"] == "replayed" and state["visibility"] == "ORACLE_ONLY"
        trace = service.traces(run, "pmt.power_w", 10200, 11000, 800, "research")
        assert trace["values"] == expected["power_w"][:, 0].tolist()
        assert trace["status"] == "replayed" and trace["visibility"] == "ORACLE_ONLY"
        assert (
            service.traces(run, "pmt.power_w", 10200, 11000, 800, "instrument")["status"]
            == "not_recorded"
        )
    assert hashes(out) == before


def test_unknown_tail_is_excluded_and_registration_remains_frozen(tmp_path, monkeypatch):
    out = source(tmp_path, monkeypatch)
    with saved_service(out) as service:
        run = service.runs["recording-c0"]
        expected = service.traces(run, "adc.code", 0, 11000, 40, "instrument")
        with (out / "raw.zlraw").open("ab") as f:
            f.write(b"not a committed block")
        with (out / "events.jsonl").open("ab") as f:
            f.write(b"not committed diagnostics")
        assert service.traces(run, "adc.code", 0, 11000, 40, "instrument") == expected
        assert service.manifest(run)["steps"] == 11000
        assert service.manifest(run)["uncommitted_tail_present"]


def test_committed_prefix_mutation_is_refused_after_registration(tmp_path, monkeypatch):
    out = source(tmp_path, monkeypatch)
    with saved_service(out) as service:
        raw = out / "raw.zlraw"
        data = bytearray(raw.read_bytes())
        data[-1] ^= 1
        raw.write_bytes(data)
        with pytest.raises(ValueError, match="SHA"):
            service.traces(service.runs["recording-c0"], "adc.code", 0, 11000, 40, "instrument")


def test_observable_events_and_lineage_do_not_invent_host_times(tmp_path, monkeypatch):
    out = source(tmp_path, monkeypatch)
    raw = read_all(out / "raw.zlraw")
    index = raw.blocks[-1].first_sample_index
    with saved_service(out) as service:
        run = service.runs["recording-c0"]
        line = service.lineage(run, index, "instrument")
        assert line["status"] == "exact_record" and line["visibility"] == "OBSERVABLE"
        assert line["lineage"]["host_arrival_time"] is None
        assert line["lineage"]["first_sample_index"] == str(raw.blocks[-1].first_sample_index)
        assert service.lineage(run, 0, "instrument")["status"] == "not_recorded"
        events = service.events(run, 0, 11000, None, "instrument")
        assert events["status"] == "not_recorded"
        assert all(e["kind"] == "epoch_observed" for e in events["events"])
        effects = service.events(run, 0, 11000, {"control_effective"}, "research")
        assert effects["events"][0]["step"] == 10321
        assert effects["events"][0]["visibility"] == "ORACLE_ONLY"


def test_second_channel_research_stays_on_its_own_chain(tmp_path, monkeypatch):
    from zlscan.engine.recording_replay import RecordingReplay

    out = source(tmp_path, monkeypatch)
    with saved_service(out) as service, RecordingReplay(out) as original:
        expected = original.replay(10200, 800, {"power_w"})
        run = service.runs["recording-c1"]
        result = service.replay(run, 10200, 800, ["power_w"])
        assert result["codes"] == expected["codes"][:, 1].tolist()
        assert result["probes"]["pmt.power_w"] == expected["power_w"][:, 1].tolist()
        assert result["probes"]["pmt.power_w"] != expected["power_w"][:, 0].tolist()


def test_simulated_loss_journal_is_research_only_and_not_filled_from_model(tmp_path, monkeypatch):
    out = source(
        tmp_path, monkeypatch, stages=[{"name": "too_small", "capacity_bytes": 1, "rate_Bps": 1e9}]
    )
    with saved_service(out) as service:
        run = service.runs["recording-c0"]
        trace = service.traces(run, "adc.code", 10200, 11000, 400, "instrument")
        assert trace["values"] == [None] * 200
        losses = service.events(run, 0, 11000, {"transport_loss"}, "research")
        assert losses["events"] and all(e["visibility"] == "ORACLE_ONLY" for e in losses["events"])
        assert service.events(run, 0, 11000, {"transport_loss"}, "instrument")["events"] == []
        assert service.replay(run, 10200, 100, [])["codes"]


def test_actual_http_snapshot_routes_and_close_release_resources(tmp_path, monkeypatch):
    import threading
    from http.client import HTTPConnection

    from zlscan.api.server import make_server

    out = source(tmp_path, monkeypatch)
    before = hashes(out)
    with saved_service(out) as service:
        server = make_server(service, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()

        def query(path, body=None):
            connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=15)
            try:
                connection.request(
                    "POST" if body else "GET", path, body=json.dumps(body) if body else None
                )
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        try:
            assert query("/runs")[1]["runs"] == ["recording-c0", "recording-c1"]
            assert query("/runs/recording-c0/manifest")[1]["frozen_commit"]
            instrument = query(
                "/runs/recording-c0/traces?signal=adc.code&start=10200&end=11000"
                "&pixel_width=20&view=instrument"
            )[1]
            assert instrument["visibility"] == "OBSERVABLE" and instrument["missing_count"] > 0
            research = query(
                "/runs/recording-c1/replay-window?view=research",
                {"start": 10200, "n_steps": 100, "probes": ["power_w"]},
            )[1]
            assert research["status"] == "replayed" and research["visibility"] == "ORACLE_ONLY"
            assert (
                query(
                    "/runs/recording-c0/replay-window?view=instrument",
                    {"start": 10200, "n_steps": 100},
                )[1]["status"]
                == "not_computed"
            )
            assert (
                query("/runs/recording-c0/branches", {"gain": 1100})[1]["status"] == "not_computed"
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=10)
        assert not thread.is_alive()
        index = service.delivered.database
    assert not index.exists()
    assert hashes(out) == before


def test_disk_index_preserves_unsigned_64_bit_source_identity(tmp_path):
    from zlscan.io.committed_adc import CommittedADC
    from zlscan.io.rawstream import RawWriter, SessionHeader

    path = tmp_path / "high.zlraw"
    header = SessionHeader(
        schema_version="0.1",
        session_id=7,
        created_at="SIMULATED",
        hardware_hash=None,
        firmware_hash=None,
        wiring_rev=None,
        register_dump_hash=None,
        calibration_id=None,
        channel_roles={"0": None},
        clock_domains=[{"id": 1, "freq_num": 1}],
        coding="offset_binary",
        bits_per_sample=16,
        bytes_per_sample=2,
        windows={},
        origin="SIMULATED",
    )
    first = 2**63 + 17
    with RawWriter(path, header) as writer:
        writer.write_block(
            epoch=0,
            channel_mask=1,
            clock_domain=1,
            first_sample_index=first,
            samples=np.arange(8, dtype=np.uint16).reshape(-1, 1),
        )
        checkpoint = json.loads(writer.checkpoint())
    before = hashes(tmp_path)
    with CommittedADC(path, checkpoint, first + 8, (0,), 1) as reader:
        result = reader.query(0, first, first + 8, 8)
        assert result["values"] == list(range(8))
        assert reader.sample(0, first + 7)["first_sample_index"] == str(first)
        assert reader.sample(0, first + 7)["value"] == 7
        assert reader.query(0, first + 3, first + 5, 8)["values"] == [3, 4]
    assert hashes(tmp_path) == before


def test_research_timing_uses_original_custom_link_and_keeps_host_unknown(tmp_path, monkeypatch):
    out = source(tmp_path, monkeypatch, link={"latency_half_cycles": 6, "propagation_ps": 1700})
    raw = read_all(out / "raw.zlraw")
    index = raw.blocks[-1].first_sample_index
    with saved_service(out) as service:
        run = service.runs["recording-c0"]
        result = service.lineage(run, index, "research")
        model = result["model_lineage"]
        assert int(model["adc_output_time"]) - int(model["sample_time"]) == 3_000_000
        assert int(model["pin_arrival_time"]) - int(model["adc_output_time"]) == 1700
        assert model["visibility"] == "ORACLE_ONLY" and model["host_arrival_time"] is None
        assert result["transport_diagnostic"]["last_stage_completion"] is not None
        assert "model_lineage" not in service.lineage(run, index, "instrument")


def test_missing_history_is_unavailable_not_a_fabricated_demo(tmp_path, monkeypatch):
    import shutil

    out = source(tmp_path, monkeypatch)
    legacy = tmp_path / "legacy"
    shutil.copytree(out, legacy, ignore=shutil.ignore_patterns("history"))
    with saved_service(legacy) as service:
        run = service.runs["recording-c0"]
        assert service.replay(run, 10200, 100, [])["status"] == "not_recorded"
        assert service.state(run, 10200, {"power_w"})["status"] == "not_recorded"
        assert service.traces(run, "adc.code", 10200, 11000, 40, "instrument")["values"]


def test_capture_observables_are_original_vectors_not_queue_delivery(tmp_path, monkeypatch):
    from zlscan.io.observable_arrays import decode_arrays

    out = source(tmp_path, monkeypatch)
    raw = read_all(out / "raw.zlraw")
    block = raw.blocks[-1]
    index = block.first_sample_index
    records = [json.loads(line) for line in (out / "events.jsonl").read_text().splitlines()]
    entry = next(
        e
        for row in records
        for e in row.get("delivered", [])
        if int(e["block_sequence"]) == block.seq
    )
    arrays = decode_arrays(entry["source"]["observables"])
    with saved_service(out) as service:
        actual = service.lineage(service.runs["recording-c0"], index, "instrument")
        assert actual["lineage"]["capture_time"] == str(int(arrays["capture_time"][0]))
        assert actual["lineage"]["capture_count"] == str(int(arrays["capture_count"][0]))
        assert actual["lineage"]["overflow"] == bool(arrays["overflow"][0])
        assert "transport_diagnostic" not in actual
        assert actual["lineage"]["host_arrival_time"] is None


def test_event_overflow_is_reported_instead_of_looking_complete(tmp_path, monkeypatch):
    out = source(tmp_path, monkeypatch)
    with saved_service(out) as service:

        def events(*args, limit=4096):
            return [dict(step=i, sample_index=str(i), kind="epoch_observed") for i in range(limit)]

        monkeypatch.setattr(service.delivered, "events", events)
        result = service.events(
            service.runs["recording-c0"], 0, 11000, {"epoch_observed"}, "instrument"
        )
        assert result["truncated"] and len(result["events"]) == 4096
