"""Exact-time state is replayed from original producer inputs, never inferred from delivery."""

import io
import json
from pathlib import Path

import numpy as np
import pytest
from tests.unit.test_recording_workbench import hashes

from zlscan.cli import main
from zlscan.engine.saved_service import saved_service


def source(tmp_path, monkeypatch, *, history=True, link=None, stages=None):
    import sys

    cfg = json.loads(Path("configs/physical_recording.json").read_text())
    cfg.update(chunk_steps=97, record_digital_history=history)
    cfg["chain"]["source"]["gated"] = False
    cfg["chain"]["detector"].update(shot_noise=True, pole_hz=70000.0)
    cfg["chain"]["psf"].update(nodes_x=1, nodes_y=1)
    cfg["acquisition"] = dict(window_start=2, window_length=13)
    if link is not None:
        cfg["link"].update(link)
    if stages is not None:
        cfg["stages"] = stages
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    rows = [
        dict(op="sync", ticks=["20000000", "50000000", "80000000"]),
        dict(op="advance", steps=57),
        dict(op="request", channel=0, parameter="detector.gain", value=2400.0, effective_step=113),
        dict(op="advance", steps=280),
        dict(op="reset"),
        dict(op="start"),
        dict(op="advance", steps=450),
        dict(op="finish"),
    ]
    monkeypatch.setattr(sys, "stdin", io.StringIO("".join(json.dumps(r) + "\n" for r in rows)))
    folder = tmp_path / "source"
    assert main(["run", "--mode", "recording", "--config", str(path), "--out", str(folder)]) == 0
    return folder


def test_partial_original_batch_uses_exact_ps_and_distinguishes_prefetch(tmp_path, monkeypatch):
    from zlscan.adc.patterns import ConvertedSamples
    from zlscan.adc_link.link import transmit
    from zlscan.contracts.time import CaptureClock
    from zlscan.engine.chain import Chain

    folder = source(tmp_path, monkeypatch)
    before = hashes(folder)
    with saved_service(folder) as service:
        run = service.runs["recording-c0"]
        tick = 25_000_001
        result = service.digital_state(run, tick)
        assert result["status"] == "replayed" and result["visibility"] == "ORACLE_ONLY"
        assert result["tick_ps"] == str(tick)
        assert result["source"]["sampled_rows"] == "26"
        assert int(result["link"]["prefetched_future_rows"]) > 0
        saved = service.snapshot
        # Independently transform known source rows, then count only rows physically sampled by t.
        cfg = saved.configs[0]
        codes = Chain(cfg).advance(113).codes[:, None]
        words = ConvertedSamples(codes, (0,), 0, 0, saved.current.domain, saved.link.bits)
        out = transmit(words, saved.link)
        in_flight = np.count_nonzero((out.sample_time <= tick) & (out.capture_time > tick))
        assert result["link"]["in_flight_rows"] == str(in_flight)
        clock = CaptureClock(
            saved.current.domain, edge=saved.link.capture_edge, phase_ticks=saved.link.dco_phase_ps
        )
        assert result["link"]["capture_count"] == str(clock.count_at(tick))
        active = result["acquisition"]["active"]
        start = clock.first_at(20_000_000) + saved.acquisition.sync_latency_cycles + 2
        retained = np.count_nonzero(
            (out.capture_time <= tick) & (out.capture_time >= clock.tick_of(start))
        )
        assert active["capture_start_index"] == str(start)
        assert active["retained_rows"] == str(retained)
        assert service.digital_state(run, tick) == result
        assert (
            service.digital_state(service.runs["recording-c1"], tick)["source"] == result["source"]
        )
    assert hashes(folder) == before


def test_state_changes_at_capture_window_and_queue_completion_edges(tmp_path, monkeypatch):
    folder = source(tmp_path, monkeypatch)
    with saved_service(folder) as service:
        run = service.runs["recording-c0"]
        a = service.digital_state(run, 36_499_999)
        b = service.digital_state(run, 36_500_000)
        assert a["acquisition"]["active"] is not None
        assert b["acquisition"]["active"] is None
        assert sum(int(s["resident_bytes"]) for s in b["transport"]["stages"]) > 0
        assert int(b["counts"]["delivered_packets"]) == 0
        c = service.digital_state(run, 60_000_000)
        assert int(c["counts"]["delivered_packets"]) == 1
        assert c["control"]["state"] == "scanning"
        assert service.digital_state(run, 90_000_001)["control"]["epoch"] > 1


def test_old_missing_history_and_instrument_never_replay_truth(tmp_path, monkeypatch):
    from zlscan.engine.chain import Chain

    folder = source(tmp_path, monkeypatch, history=False)
    with saved_service(folder) as service:

        def forbidden(*args):
            raise AssertionError("no source model should run")

        monkeypatch.setattr(Chain, "advance", forbidden)
        run = service.runs["recording-c0"]
        assert service.digital_state(run, 1)["status"] == "not_recorded"
        assert service.digital_state(run, 1, "instrument")["status"] == "not_recorded"


def test_digital_tick_must_be_signed_integer_and_inside_source_timeline(tmp_path, monkeypatch):
    folder = source(tmp_path, monkeypatch)
    with saved_service(folder) as service:
        run = service.runs["recording-c0"]
        for tick in (True, 0.5, -1, 196_750_000, 2**63):
            with pytest.raises(ValueError):
                service.digital_state(run, tick)


def test_advance_to_closes_an_exact_inclusive_tick_without_fabricating_source():
    from tests.unit.test_physical_recording import settings

    from zlscan.contracts.time import ClockDomain
    from zlscan.engine.digital_pipeline import DigitalPipeline

    _, link, acq, stages = settings()
    pipeline = DigitalPipeline(ClockDomain(1, 1_000_000), link, acq, stages, (0, 1))
    pipeline.schedule_syncs([0])
    pipeline.advance_to(1)
    assert pipeline.watermark == 1 and pipeline.link.next_index is None
    assert pipeline.acquisition.active is None  # Explicit two-cycle sync latency has not elapsed.
    pipeline.advance_to(2_000_001)
    assert pipeline.acquisition.active is not None
    with pytest.raises(ValueError):
        pipeline.advance_to(0)


def test_public_http_tick_route_and_instrument_isolation(tmp_path, monkeypatch):
    import threading

    from tests.unit.test_api import get

    from zlscan.api.server import make_server

    folder = source(tmp_path, monkeypatch)
    before = hashes(folder)
    with saved_service(folder) as service:
        server = make_server(service, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            path = "/runs/recording-c0/digital-state?tick=25000001&req=77"
            code, body = get(port, path)
            assert code == 200 and body["tick_ps"] == "25000001" and body["req"] == "77"
            assert get(port, path + "&view=instrument")[1]["status"] == "not_recorded"
            for value in ("01", "-0", "1.5", "9223372036854775808", "196750000"):
                assert get(port, "/runs/recording-c0/digital-state?tick=" + value)[0] == 400
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
    assert hashes(folder) == before


def test_partial_source_code_mismatch_is_refused_and_query_budget_is_explicit(
    tmp_path, monkeypatch
):
    import zlscan.engine.digital_state as query
    from zlscan.engine.chain import Chain

    folder = source(tmp_path, monkeypatch)
    with saved_service(folder) as service:
        run = service.runs["recording-c0"]
        with monkeypatch.context() as patch:
            patch.setattr(query, "MAX_REPLAY_STEPS", 1)
            assert service.digital_state(run, 25_000_001)["status"] == "not_computed"
        advance = Chain.advance

        def corrupted(self, steps):
            result = advance(self, steps)
            if len(result.codes):
                result.codes[0] ^= 1
            return result

        monkeypatch.setattr(Chain, "advance", corrupted)
        with pytest.raises(ValueError, match="fingerprint"):
            service.digital_state(run, 25_000_001)


@pytest.mark.parametrize("case", ["falling", "rising_lookahead", "overflow", "blocked"])
def test_query_matches_independent_chronological_pipeline_across_epochs_and_queues(
    tmp_path, monkeypatch, case
):
    from zlscan.adc.patterns import ConvertedSamples
    from zlscan.control.state import split_by_epoch
    from zlscan.engine.digital_pipeline import DigitalPipeline

    link = (
        dict(capture_edge="rising", overflow_offset_cycles=-2)
        if case == "rising_lookahead"
        else None
    )
    stages = None
    if case == "overflow":
        stages = [dict(name="disk", capacity_bytes=80, rate_Bps=1e7)]
    if case == "blocked":
        stages = [
            dict(name="rx", capacity_bytes=232, rate_Bps=1e9),
            dict(name="disk", capacity_bytes=116, rate_Bps=1e6, stalls=[[40_000_000, 50_000_000]]),
        ]
    folder = source(tmp_path, monkeypatch, link=link, stages=stages)
    before = hashes(folder)
    with saved_service(folder) as service:
        saved = service.snapshot
        model = saved.replay(0, saved.step, set())
        reference = DigitalPipeline(
            saved.current.domain,
            saved.link,
            saved.acquisition,
            saved.stages,
            saved.current.channels,
        )
        reference.schedule_syncs([20_000_000, 50_000_000, 80_000_000])
        source_rows = ConvertedSamples(
            model["codes"],
            saved.current.channels,
            0,
            0,
            saved.current.domain,
            saved.link.bits,
            **saved.current.identity,
        )
        for part in split_by_epoch(source_rows, saved.current.controller):
            reference.link.push(part)
        delivered = lost = retained = 0
        ticks = sorted({*range(0, 110_000_001, 500_000), 25_000_001, 84_250_001})
        for tick in ticks:
            reference.advance_to(tick)
            batch = reference.drain()
            delivered += len(batch.delivered)
            lost += len(batch.losses)
            retained += sum(len(p.packet.source) for p in batch.delivered)
            actual = service.digital_state(service.runs["recording-c0"], tick)
            assert actual["counts"] == dict(
                delivered_packets=str(delivered),
                lost_packets=str(lost),
                retained_rows=str(retained),
            )
            assert [int(s["resident_bytes"]) for s in actual["transport"]["stages"]] == [
                s.bytes for s in reference.transport.stages
            ]
            assert [int(s["overflow_count"]) for s in actual["transport"]["stages"]] == [
                s.stats.overflow_count for s in reference.transport.stages
            ]
            window = reference.acquisition.active
            active = actual["acquisition"]["active"]
            assert (active is None) == (window is None)
            if window is not None:
                assert int(active["retained_rows"]) == sum(len(s) for s in window.segments)
                assert active["epochs"] == sorted({s.epoch for s in window.segments})
                assert int(active["line_number"]) == window.line_number
    assert hashes(folder) == before
