"""Unknown-final-length public physical recording and its durable recovery protocol."""

import hashlib
import io
import json
import subprocess
import sys

import pytest

from zlscan.cli import main


def config(tmp_path, **changes):
    value = {
        "format": "zlscan.physical-recording/1",
        "model_version": "chain-0.3",
        "preset": "nominal",
        "chain": {
            "seed": 3,
            "detector": {"shot_noise": False},
            "psf": {"nodes_x": 1, "nodes_y": 1},
        },
        "secondary": {"seed": 31},
        "link": {"latency_half_cycles": 25, "propagation_ps": 1500, "capture_edge": "falling"},
        "acquisition": {"window_start": 2, "window_length": 13},
        "stages": [{"name": "disk", "capacity_bytes": 4096, "rate_Bps": 1e9}],
        "session_id": 19,
        "run_id": "physical",
        "branch_id": "main",
        "chunk_steps": 97,
    }
    value.update(changes)
    path = tmp_path / "recording.json"
    path.write_text(json.dumps(value))
    return path


def run_cli(monkeypatch, args, commands):
    monkeypatch.setattr(sys, "stdin", io.StringIO("".join(json.dumps(c) + "\n" for c in commands)))
    return main(["run", "--mode", "recording", *args])


def hashes(path):
    return {
        p.relative_to(path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in path.rglob("*")
        if p.is_file()
    }


def test_pause_resume_matches_continuous_files_and_full_root(tmp_path, monkeypatch, capsys):
    cfg = config(tmp_path)
    pre = [
        {"op": "lines", "count": 1},
        {
            "op": "request",
            "channel": 0,
            "parameter": "source.power_w",
            "value": 0.0004,
            "effective_step": "10321",
        },
        {"op": "advance", "steps": "137"},
    ]
    post = [
        {"op": "advance", "steps": 263},
        {"op": "reset"},
        {"op": "start"},
        {"op": "advance", "steps": 9600},
        {"op": "lines", "count": 2},
        {"op": "finish"},
    ]
    baseline = tmp_path / "baseline"
    actual = tmp_path / "actual"
    assert run_cli(monkeypatch, ["--config", str(cfg), "--out", str(baseline)], pre + post) == 0
    assert run_cli(monkeypatch, ["--config", str(cfg), "--out", str(actual)], pre) == 0
    paused = json.loads((actual / "run.json").read_text())
    assert paused["status"] == "paused" and paused["summary"]["fine_step"] == "10137"
    assert not paused["summary"]["closed"]
    assert paused["summary"]["written_blocks"] == 0
    assert paused["capture_sha256"] is None
    assert not (actual / "capture.json").exists()
    inode = (actual / "raw.zlraw").stat().st_ino
    cfg.write_text("external config is no longer available")
    assert run_cli(monkeypatch, ["--resume", str(actual)], post) == 0
    assert (actual / "raw.zlraw").stat().st_ino == inode
    for name in ["raw.zlraw", "events.jsonl", "checkpoint.json", "capture.json"]:
        assert (actual / name).read_bytes() == (baseline / name).read_bytes()
    final = json.loads((actual / "run.json").read_text())
    assert final["status"] == "complete" and final["summary"]["closed"]
    assert (
        final["capture_sha256"]
        == hashlib.sha256((actual / "capture.json").read_bytes()).hexdigest()
    )
    rows = [json.loads(s) for s in capsys.readouterr().out.splitlines()]
    assert all(r["checkpoint_sha256"] for r in rows)
    assert rows[-1]["summary"]["fine_step"] == "40000"


def test_unknown_tail_refuses_in_place_and_branch_preserves_original(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    source = tmp_path / "source"
    assert (
        run_cli(
            monkeypatch, ["--config", str(cfg), "--out", str(source)], [{"op": "lines", "count": 1}]
        )
        == 0
    )
    with (source / "raw.zlraw").open("ab") as f:
        f.write(b"unknown uncommitted bytes")
    before = hashes(source)
    assert run_cli(monkeypatch, ["--resume", str(source)], [{"op": "lines", "count": 1}]) == 1
    assert hashes(source) == before
    branch = tmp_path / "branch"
    assert (
        run_cli(
            monkeypatch,
            ["--resume", str(source), "--out", str(branch)],
            [{"op": "lines", "count": 1}, {"op": "finish"}],
        )
        == 0
    )
    assert hashes(source) == before
    assert json.loads((branch / "run.json").read_text())["status"] == "complete"


def test_paused_capture_binds_delivered_bytes_and_resume_updates_snapshot(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    path = tmp_path / "paused"
    assert (
        run_cli(
            monkeypatch, ["--config", str(cfg), "--out", str(path)], [{"op": "lines", "count": 3}]
        )
        == 0
    )
    capture = json.loads((path / "capture.json").read_text())
    run = json.loads((path / "run.json").read_text())
    assert run["status"] == "paused"
    assert capture["version"] == 2
    assert capture["raw_sha256"] == hashlib.sha256((path / "raw.zlraw").read_bytes()).hexdigest()
    assert run["capture_sha256"] == hashlib.sha256((path / "capture.json").read_bytes()).hexdigest()
    assert (
        run_cli(
            monkeypatch, ["--resume", str(path)], [{"op": "lines", "count": 1}, {"op": "finish"}]
        )
        == 0
    )
    assert capture["raw_sha256"] != hashlib.sha256((path / "raw.zlraw").read_bytes()).hexdigest()
    updated = json.loads((path / "capture.json").read_text())
    assert updated["raw_sha256"] == hashlib.sha256((path / "raw.zlraw").read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "changes",
    [
        {"link": {}},
        {"stages": [{"name": "disk", "capacity_bytes": None, "rate_Bps": 1e9}]},
        {"chunk_steps": True},
        {"lines": 2},
        {"link": {"latency_half_cycles": 1, "propagation_ps": 0, "unexpected": True}},
        {"stages": []},
    ],
)
def test_invalid_recording_definition_creates_no_output(tmp_path, monkeypatch, changes):
    cfg = config(tmp_path, **changes)
    out = tmp_path / "out"
    assert run_cli(monkeypatch, ["--config", str(cfg), "--out", str(out)], []) == 1
    assert not out.exists()


@pytest.mark.parametrize(
    "command",
    [
        {"op": "advance", "steps": True},
        {"op": "advance", "steps": 0},
        {"op": "advance", "steps": 10, "extra": 1},
        {"op": "unknown"},
        {"op": "sync", "ticks": ["0"] * 129},
    ],
)
def test_bad_commands_keep_initial_root_and_fail(tmp_path, monkeypatch, command):
    cfg = config(tmp_path)
    out = tmp_path / "out"
    assert run_cli(monkeypatch, ["--config", str(cfg), "--out", str(out)], [command]) == 1
    assert json.loads((out / "checkpoint.json").read_text())["chains"]["0"]["step"] == "0"


def test_failed_mid_batch_never_publishes_partial_physics(tmp_path, monkeypatch):
    from zlscan.engine.physical_recording import PhysicalRecorder

    original = PhysicalRecorder.advance
    calls = 0

    def fail(self, count):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected disk full")
        return original(self, count)

    monkeypatch.setattr(PhysicalRecorder, "advance", fail)
    cfg = config(tmp_path)
    out = tmp_path / "out"
    assert (
        run_cli(
            monkeypatch, ["--config", str(cfg), "--out", str(out)], [{"op": "lines", "count": 1}]
        )
        == 1
    )
    assert json.loads((out / "checkpoint.json").read_text())["chains"]["0"]["step"] == "0"


def test_subprocess_stdin_eof_is_pause_and_fresh_resume_finishes(tmp_path):
    cfg = config(tmp_path)
    out = tmp_path / "out"
    first = subprocess.run(
        [
            sys.executable,
            "-m",
            "zlscan",
            "run",
            "--mode",
            "recording",
            "--config",
            str(cfg),
            "--out",
            str(out),
        ],
        input='{"op":"lines","count":1}\n',
        capture_output=True,
        text=True,
    )
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout.splitlines()[-1])["status"] == "paused"
    second = subprocess.run(
        [sys.executable, "-m", "zlscan", "run", "--mode", "recording", "--resume", str(out)],
        input='{"op":"lines","count":1}\n{"op":"finish"}\n',
        capture_output=True,
        text=True,
    )
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout.splitlines()[-1])["status"] == "complete"


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX signal delivery; Windows uses TerminateProcess"
)
def test_actual_sigint_pauses_at_safe_boundary_and_can_resume(tmp_path):
    import os
    import signal
    import time

    cfg = config(tmp_path)
    out = tmp_path / "signal"
    p = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "zlscan",
            "run",
            "--mode",
            "recording",
            "--config",
            str(cfg),
            "--out",
            str(out),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        opened = json.loads(p.stdout.readline())
        assert opened["op"] == "open"
        p.stdin.write('{"op":"lines","count":100000}\n')
        p.stdin.flush()
        deadline = time.monotonic() + 15
        while (out / "raw.zlraw").stat().st_size <= 2000 and time.monotonic() < deadline:
            time.sleep(0.01)
        os.kill(p.pid, signal.SIGINT)
        stdout, stderr = p.communicate(timeout=20)
        assert p.returncode == 130, stderr
        terminal = json.loads(stdout.splitlines()[-1])
        assert terminal["status"] == "paused" and not terminal["summary"]["closed"]
        assert (
            terminal["checkpoint_sha256"]
            == hashlib.sha256((out / "checkpoint.json").read_bytes()).hexdigest()
        )
        step = int(terminal["summary"]["fine_step"])
        assert 0 < step < 100000 * 10000
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
    resumed = subprocess.run(
        [sys.executable, "-m", "zlscan", "run", "--mode", "recording", "--resume", str(out)],
        input='{"op":"finish"}\n',
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads(resumed.stdout.splitlines()[-1])["status"] == "complete"


def test_control_and_pending_sync_history_limits_do_not_grow_silently(tmp_path, monkeypatch):
    cfg = config(tmp_path, max_control_events=16)
    out = tmp_path / "bounded"
    commands = [
        {
            "op": "request",
            "channel": 0,
            "parameter": "source.power_w",
            "value": 0.0001,
            "effective_step": 0,
        }
        for _ in range(30)
    ]
    assert run_cli(monkeypatch, ["--config", str(cfg), "--out", str(out)], commands) == 1
    root = json.loads((out / "checkpoint.json").read_text())
    assert len(root["commands"]) < 30
    assert len(root["control"]["log"]) <= 16


def test_many_incremental_lines_keep_root_size_and_live_memory_bounded(tmp_path, monkeypatch):
    import tracemalloc

    from zlscan.engine.physical_recording import PhysicalRecorder

    cfg = config(
        tmp_path,
        chain={
            "scan": {"rpm": 600000.0},
            "detector": {"shot_noise": False},
            "psf": {"nodes_x": 1, "nodes_y": 1},
        },
        chunk_steps=97,
    )
    observed = []
    original = PhysicalRecorder.advance

    def observe(self, count):
        result = original(self, count)
        if self.step in [10000, 100000, 200000]:
            observed.append((self.step, tracemalloc.get_traced_memory()[0], len(self.checkpoint())))
        return result

    monkeypatch.setattr(PhysicalRecorder, "advance", observe)
    tracemalloc.start()
    try:
        assert (
            run_cli(
                monkeypatch,
                ["--config", str(cfg), "--out", str(tmp_path / "long")],
                [{"op": "lines", "count": 2000}, {"op": "finish"}],
            )
            == 0
        )
    finally:
        tracemalloc.stop()
    assert len(observed) == 3
    # Late live Python allocations do not retain 1,900 additional lines; 512 KiB allowance
    # covers transient interpreter caches. This is not a total OS RSS or arbitrary-scene bound.
    assert observed[-1][1] - observed[0][1] < 512 * 1024
    assert abs(observed[-1][2] - observed[0][2]) < 2048


def test_control_budget_leaves_room_to_restart_and_finish(tmp_path, monkeypatch):
    cfg = config(tmp_path, max_control_events=16)
    out = tmp_path / "limited"
    assert (
        run_cli(
            monkeypatch,
            ["--config", str(cfg), "--out", str(out)],
            [{"op": "reset"}, {"op": "start"}],
        )
        == 0
    )
    assert run_cli(monkeypatch, ["--resume", str(out)], [{"op": "reset"}]) == 1
    assert run_cli(monkeypatch, ["--resume", str(out)], [{"op": "finish"}]) == 0
    root = json.loads((out / "checkpoint.json").read_text())
    assert len(root["control"]["log"]) <= 16


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX signal delivery; Windows uses TerminateProcess"
)
def test_sigterm_while_waiting_for_input_commits_pause(tmp_path):
    import signal

    cfg = config(tmp_path)
    out = tmp_path / "idle_signal"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "zlscan",
            "run",
            "--mode",
            "recording",
            "--config",
            str(cfg),
            "--out",
            str(out),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert json.loads(proc.stdout.readline())["op"] == "open"
        proc.send_signal(signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=20)
        assert proc.returncode == 128 + signal.SIGTERM, stderr
        assert json.loads(stdout.splitlines()[-1])["status"] == "paused"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
