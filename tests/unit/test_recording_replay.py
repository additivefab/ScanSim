"""Historical physical replay uses recorded roots and the actual timed control plan."""

import hashlib
import io
import json
import shutil
import sys
from dataclasses import replace

import numpy as np
import pytest

from zlscan.cli import main


def hashes(path):
    return {
        p.relative_to(path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in path.rglob("*")
        if p.is_file()
    }


def make_run(tmp_path, monkeypatch):
    data = {
        "format": "zlscan.physical-recording/1",
        "model_version": "chain-0.3",
        "preset": "nominal",
        "chain": {
            "seed": 3,
            "source": {"gated": False},
            "detector": {"shot_noise": True, "pole_hz": 70000.0},
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
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    commands = [
        {"op": "lines", "count": 1},
        {
            "op": "request",
            "channel": 0,
            "parameter": "source.power_w",
            "value": 0.0002,
            "effective_step": 10321,
        },
        {
            "op": "request",
            "channel": 1,
            "parameter": "detector.gain",
            "value": 2400.0,
            "effective_step": 10407,
        },
        {"op": "sync", "ticks": ["2500007000"]},
        {"op": "advance", "steps": 450},
        {"op": "reset"},
        {"op": "start"},
        {"op": "advance", "steps": 550},
        {"op": "finish"},
    ]
    monkeypatch.setattr(sys, "stdin", io.StringIO("".join(json.dumps(c) + "\n" for c in commands)))
    source = tmp_path / "recorded"
    assert main(["run", "--mode", "recording", "--config", str(path), "--out", str(source)]) == 0
    return source


def test_control_aware_multichannel_replay_matches_independent_chain_and_final_state(
    tmp_path, monkeypatch
):
    from zlscan.engine.chain import Chain
    from zlscan.engine.recording_replay import RecordingReplay

    source = make_run(tmp_path, monkeypatch)
    before = hashes(source)
    with RecordingReplay(source) as saved:
        r = saved.replay(10200, 800, {"power_w", "current_a", "v_adc"})
        assert r["metadata"]["checkpoint_step"] == 10000
        assert r["metadata"]["channels"] == [0, 1]
        assert r["metadata"]["visibility"] == "ORACLE_ONLY"
        assert r["metadata"]["status"] == "replayed"
        assert len(np.unique(r["epochs"])) >= 2
        # power_w probes the collected PMT input, after the declared optical efficiencies.
        optical = saved.configs[0].collect
        eta = optical.eta_pre * optical.eta_collect
        np.testing.assert_array_equal(r["power_w"][:121, 0], np.full(121, 0.001 * eta))
        np.testing.assert_array_equal(r["power_w"][121:, 0], np.full(679, 0.0002 * eta))
        for i, cfg in saved.configs.items():
            chain = Chain(replace(cfg))
            pieces = []
            for stop in [10321, 10407, 11000]:
                pieces.append(chain.advance(stop - chain.step))
                if stop == 10321 and i == 0:
                    chain.cfg.source = replace(chain.cfg.source, power_w=0.0002)
                if stop == 10407 and i == 1:
                    chain.cfg.detector = replace(chain.cfg.detector, gain=2400.0)
            whole = np.concatenate([p.codes for p in pieces])
            np.testing.assert_array_equal(r["codes"][:, i], whole[2550:2750])
            assert json.loads(r["final_states"][str(i)]) == json.loads(chain.checkpoint())
        from zlscan.io.rawstream import read_all

        delivered = read_all(source / "raw.zlraw")
        observed = 0
        for segment in delivered.segments:
            lo = max(2550, segment.first_sample_index)
            hi = min(2750, segment.first_sample_index + len(segment.samples))
            if hi > lo:
                np.testing.assert_array_equal(
                    r["codes"][lo - 2550 : hi - 2550],
                    segment.samples[
                        lo - segment.first_sample_index : hi - segment.first_sample_index
                    ],
                )
                np.testing.assert_array_equal(
                    r["epochs"][lo - 2550 : hi - 2550], np.full(hi - lo, segment.epoch)
                )
                observed += hi - lo
        assert observed > 0
    assert hashes(source) == before


def test_cli_publishes_oracle_window_without_mutating_recording(tmp_path, monkeypatch):
    source = make_run(tmp_path, monkeypatch)
    before = hashes(source)
    out = tmp_path / "replay"
    assert (
        main(
            [
                "replay",
                "--run",
                str(source),
                "--start-step",
                "10200",
                "--steps",
                "800",
                "--probes",
                "power_w,v_adc",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    m = json.loads((out / "run.json").read_text())
    assert m["visibility"] == "ORACLE_ONLY" and m["checkpoint_step"] == 10000
    with np.load(out / "samples.npz", allow_pickle=False) as f:
        assert f["codes"].shape == (200, 2)
        assert f["power_w"].shape == (800, 2)
        assert f["epochs"].shape == (200,)
    assert hashes(source) == before


def test_loader_never_advances_model_or_opens_output_writer(tmp_path, monkeypatch):
    from zlscan.engine.chain import Chain
    from zlscan.engine.digital_recording import DigitalRecorder
    from zlscan.engine.recording_replay import RecordingReplay

    source = make_run(tmp_path, monkeypatch)
    before = hashes(source)

    def forbidden(*a, **kw):
        raise AssertionError("read-only registration must not produce data")

    monkeypatch.setattr(Chain, "advance", forbidden)
    monkeypatch.setattr(DigitalRecorder, "restore", forbidden)
    monkeypatch.setattr(DigitalRecorder, "__init__", forbidden)
    with RecordingReplay(source) as saved:
        assert saved.step == 11000
    assert hashes(source) == before


def test_unknown_uncommitted_tail_is_preserved_and_not_replayed(tmp_path, monkeypatch):
    from zlscan.engine.recording_replay import RecordingReplay

    source = make_run(tmp_path, monkeypatch)
    with (source / "raw.zlraw").open("ab") as f:
        f.write(b"unknown raw tail")
    with (source / "events.jsonl").open("ab") as f:
        f.write(b"unknown journal tail")
    before = hashes(source)
    with RecordingReplay(source) as saved:
        r = saved.replay(10200, 800, set())
        assert r["metadata"]["uncommitted_tail_present"]
    assert hashes(source) == before


def test_missing_history_does_not_manufacture_an_initial_checkpoint(tmp_path, monkeypatch):
    source = make_run(tmp_path, monkeypatch)
    legacy = tmp_path / "legacy"
    shutil.copytree(source, legacy, ignore=shutil.ignore_patterns("history"))
    assert (
        main(
            [
                "replay",
                "--run",
                str(legacy),
                "--start-step",
                "100",
                "--steps",
                "100",
                "--out",
                str(tmp_path / "refused"),
            ]
        )
        == 1
    )
    assert not (tmp_path / "refused").exists()


@pytest.mark.parametrize("damage", ["history", "raw", "journal", "definition"])
def test_corrupt_artifact_refuses_and_keeps_source(tmp_path, monkeypatch, damage):
    source = make_run(tmp_path, monkeypatch)
    if damage == "history":
        path = next((source / "history").glob("00000000000000010000-*.json"))
    else:
        path = (
            source
            / (
                {"raw": "raw.zlraw", "journal": "events.jsonl", "definition": "definition.json"}[
                    damage
                ]
            )
        )
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 1
    path.write_bytes(data)
    before = hashes(source)
    assert (
        main(
            [
                "replay",
                "--run",
                str(source),
                "--start-step",
                "10200",
                "--steps",
                "100",
                "--out",
                str(tmp_path / "refused"),
            ]
        )
        == 1
    )
    assert hashes(source) == before
    assert not (tmp_path / "refused").exists()


def test_new_history_root_and_branch_copy_do_not_modify_original(tmp_path, monkeypatch):
    source = make_run(tmp_path, monkeypatch)
    roots = list((source / "history").glob("*.json"))
    assert len(roots) >= 6
    for p in roots:
        assert hashlib.sha256(p.read_bytes()).hexdigest() == p.stem.split("-")[1]
    before = hashes(source)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    branch = tmp_path / "branch"
    assert main(["run", "--mode", "recording", "--resume", str(source), "--out", str(branch)]) == 0
    assert {p.name for p in roots} <= {p.name for p in (branch / "history").glob("*.json")}
    assert hashes(source) == before


@pytest.mark.parametrize(
    "start,steps", [(0, 1), (1, 2), (3, 1), (10321, 1), (10407, 4), (10449, 5), (10999, 1)]
)
def test_short_unaligned_windows_keep_probe_and_adc_support_separate(
    tmp_path, monkeypatch, start, steps
):
    from zlscan.engine.recording_replay import RecordingReplay

    source = make_run(tmp_path, monkeypatch)
    with RecordingReplay(source) as saved:
        r = saved.replay(start, steps, {"power_w"})
        first = -(-start // 4)
        last = -(-(start + steps) // 4)
        assert r["codes"].shape == (last - first, 2)
        assert r["power_w"].shape == (steps, 2)
        assert r["epochs"].shape == (last - first,)


def test_immutable_input_change_after_registration_is_rejected(tmp_path, monkeypatch):
    from zlscan.engine.recording_replay import RecordingReplay

    source = make_run(tmp_path, monkeypatch)
    with RecordingReplay(source) as saved:
        path = source / "definition.json"
        path.write_bytes(path.read_bytes() + b" ")
        with pytest.raises(ValueError, match="input changed"):
            saved.replay(10200, 100, set())


def test_stale_or_missing_human_manifest_does_not_replace_authoritative_root(tmp_path, monkeypatch):
    source = make_run(tmp_path, monkeypatch)
    snapshot = tmp_path / "without_manifest"
    shutil.copytree(source, snapshot, ignore=shutil.ignore_patterns("run.json"))
    assert (
        main(
            [
                "replay",
                "--run",
                str(snapshot),
                "--start-step",
                "10200",
                "--steps",
                "100",
                "--out",
                str(tmp_path / "replayed"),
            ]
        )
        == 0
    )


@pytest.mark.parametrize("start,steps", [(-1, 100), (0, 0), (10999, 2), (0, 1000001)])
def test_query_budget_and_committed_span_are_enforced(tmp_path, monkeypatch, start, steps):
    source = make_run(tmp_path, monkeypatch)
    assert (
        main(
            [
                "replay",
                "--run",
                str(source),
                "--start-step",
                str(start),
                "--steps",
                str(steps),
                "--out",
                str(tmp_path / "refused"),
            ]
        )
        == 1
    )
    assert not (tmp_path / "refused").exists()


def test_rehashed_root_cannot_read_session_bytes_from_uncommitted_tail(tmp_path, monkeypatch):
    import struct

    from zlscan.engine.physical_recording import PhysicalRecorder
    from zlscan.engine.recording_replay import RecordingReplay
    from zlscan.io.rawstream import PRELUDE

    source = make_run(tmp_path, monkeypatch)
    raw = (source / "raw.zlraw").read_bytes()
    size = struct.unpack("<I", raw[len(PRELUDE) : len(PRELUDE) + 4])[0]
    # Deliberately claim a prefix ending seven bytes before the session JSON ends.
    limit = size + 5
    root = json.loads((source / "checkpoint.json").read_text())
    writer = root["recording"]["writer"]
    writer["file"] = {
        "offset": str(limit),
        "prefix_sha256": hashlib.sha256(raw[:limit]).hexdigest(),
    }
    for document in [writer, root["recording"], root]:
        document["checksum"] = PhysicalRecorder._hash(
            {k: v for k, v in document.items() if k != "checksum"}
        )
    (source / "checkpoint.json").write_text(json.dumps(root, sort_keys=True))
    with pytest.raises(ValueError, match="prefix"):
        with RecordingReplay(source):
            pass
