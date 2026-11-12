"""Bounded JSONL control of an unknown-final-length physical recorder."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import signal
import threading
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any, Literal, TextIO

import numpy as np
from pydantic import Field, TypeAdapter

from zlscan.adc_link.link import LinkConfig
from zlscan.digital.acquisition import AcquisitionConfig
from zlscan.engine.config_bundle import config_bundle, save_config_bundle
from zlscan.engine.digital_pipeline import DigitalPipeline
from zlscan.engine.physical_recording import PhysicalRecorder
from zlscan.engine.physical_run import line_steps
from zlscan.engine.research_config import PhysicsInput, _pairs, resolve_physics
from zlscan.io.committed import publish_document
from zlscan.io.manifest import sha256_file
from zlscan.io.probe_store import TracePolicy
from zlscan.transport.queues import StageSpec
from zlscan.truth.scan import line_start_s

MAX_COMMAND_BYTES = 65536


class RecordingInput(PhysicsInput):
    format: Literal["zlscan.physical-recording/1"]
    link: dict[str, Any]
    acquisition: dict[str, Any]
    stages: list[dict[str, Any]] = Field(min_length=1)
    session_id: int = Field(ge=0, le=2**64 - 1)
    run_id: str = Field(min_length=1)
    branch_id: str = Field(min_length=1)
    chunk_steps: int = Field(gt=0, le=1_000_000)
    max_pending_syncs: int = Field(default=128, gt=0, le=4096)
    max_control_events: int = Field(default=1024, ge=16, le=4096)
    trace: TracePolicy | None = None


def _json(data):
    value = json.loads(data, object_pairs_hook=_pairs)
    json.dumps(value, allow_nan=False)
    return value


def _component(cls, data):
    if not isinstance(data, dict) or set(data) - {f.name for f in fields(cls)}:
        raise ValueError(f"unknown fields in {cls.__name__}")
    return TypeAdapter(cls).validate_json(json.dumps(data, allow_nan=False), strict=True)


def _runtime(spec):
    link = _component(LinkConfig, spec.link)
    acq = _component(AcquisitionConfig, spec.acquisition)
    stages = [_component(StageSpec, row) for row in spec.stages]
    if link.latency_half_cycles is None or link.propagation_ps is None:
        raise ValueError("recording requires explicit assumed link latency and propagation")
    if any(s.capacity_bytes is None for s in stages):
        raise ValueError("recording requires finite capacity at every stage")
    return link, acq, stages


def _create(directory, path):
    original = path.read_bytes()
    spec = RecordingInput.model_validate_json(json.dumps(_json(original)), strict=True)
    primary, secondary = resolve_physics(spec, original, path.parent, lines=None)
    configs = {0: primary, **({1: secondary} if secondary else {})}
    link, acq, stages = _runtime(spec)
    # Validate the full composition before any output is created.
    owned = PhysicalRecorder._configs(configs, link)
    from zlscan.engine.chain import Chain

    DigitalPipeline(Chain(primary).adc_clock, link, acq, stages, tuple(owned))
    recorder = PhysicalRecorder(
        directory,
        configs,
        link,
        acq,
        stages,
        session_id=spec.session_id,
        run_id=spec.run_id,
        branch_id=spec.branch_id,
        trace_policy=spec.trace,
    )
    try:
        inputs = directory / "inputs"
        inputs.mkdir()
        (inputs / "recording.json").write_bytes(original)
        definition = dict(
            format="zlscan.recording-definition/1",
            origin="SIMULATED",
            visibility="ORACLE_ONLY",
            input_sha256=hashlib.sha256(original).hexdigest(),
            link=asdict(link),
            acquisition=asdict(acq),
            stages=[asdict(s) for s in stages],
            chunk_steps=spec.chunk_steps,
            max_pending_syncs=spec.max_pending_syncs,
            max_control_events=spec.max_control_events,
            channels=[],
        )
        for channel, cfg in configs.items():
            relative = f"inputs/channel_{channel}"
            definition["channels"].append(
                dict(
                    channel=channel,
                    path=relative,
                    config_sha256=save_config_bundle(directory / relative, cfg),
                    config_hash=cfg.hash(),
                )
            )
        data = json.dumps(definition, sort_keys=True, allow_nan=False).encode()
        publish_document(directory, "definition.json", data)
        publish_document(directory, "definition.sha256", hashlib.sha256(data).hexdigest().encode())
        return recorder, definition
    except BaseException:
        recorder.__exit__()
        raise


def read_definition(source, stack):
    """Load immutable model/runtime inputs without opening an append writer."""
    data = (source / "definition.json").read_bytes()
    if hashlib.sha256(data).hexdigest() != (source / "definition.sha256").read_text():
        raise ValueError("recording definition SHA mismatch")
    definition = _json(data)
    if (
        definition.get("format") != "zlscan.recording-definition/1"
        or definition.get("origin") != "SIMULATED"
        or definition.get("visibility") != "ORACLE_ONLY"
    ):
        raise ValueError("unsupported recording definition")
    original = (source / "inputs/recording.json").read_bytes()
    if hashlib.sha256(original).hexdigest() != definition["input_sha256"]:
        raise ValueError("original recording input SHA mismatch")
    configs = {}
    for row in definition["channels"]:
        channel = row["channel"]
        if channel in configs or row["path"] != f"inputs/channel_{channel}":
            raise ValueError("invalid recording channel snapshot")
        cfg = stack.enter_context(config_bundle(source / row["path"], row["config_sha256"]))
        if cfg.hash() != row["config_hash"]:
            raise ValueError("recording configuration hash mismatch")
        configs[channel] = cfg
    # Revalidate software limits and full runtime fields from the immutable input snapshot.
    spec = RecordingInput.model_validate_json(json.dumps(_json(original)), strict=True)
    if set(configs) != ({0, 1} if spec.secondary is not None else {0}):
        raise ValueError("recording channels differ from original input")
    link, acq, stages = _runtime(spec)
    for name, expected in [
        ("link", asdict(link)),
        ("acquisition", asdict(acq)),
        ("stages", [asdict(s) for s in stages]),
        ("chunk_steps", spec.chunk_steps),
        ("max_pending_syncs", spec.max_pending_syncs),
        ("max_control_events", spec.max_control_events),
    ]:
        if json.dumps(definition[name], sort_keys=True) != json.dumps(expected, sort_keys=True):
            raise ValueError("recording runtime definition differs from input snapshot")
    return definition, configs, link, acq, stages, spec, data


def _restore(source, target, stack):
    definition, configs, link, acq, stages, spec, data = read_definition(source, stack)
    recorder = PhysicalRecorder.restore(
        source,
        configs,
        link,
        acq,
        stages,
        (source / "checkpoint.json").read_bytes(),
        branch_directory=target if target != source else None,
    )
    try:
        if (recorder.trace.policy if recorder.trace is not None else None) != spec.trace:
            raise ValueError("probe recording policy differs from saved input")
        if recorder.identity != dict(
            session_id=spec.session_id, run_id=spec.run_id, branch_id=spec.branch_id
        ):
            raise ValueError("recording source identity differs")
        if target != source:
            shutil.copytree(source / "inputs", target / "inputs")
            if (source / "history").is_dir():
                shutil.copytree(source / "history", target / "history")
            publish_document(target, "definition.json", data)
            publish_document(target, "definition.sha256", hashlib.sha256(data).hexdigest().encode())
        return recorder, definition
    except BaseException:
        recorder.__exit__()
        raise


def _positive(value):
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        value = int(value)
    if type(value) is not int or value <= 0:
        raise ValueError("count must be a positive integer or decimal string")
    return value


def _nonnegative(value):
    if value == 0 and type(value) is int or value == "0":
        return 0
    return _positive(value)


class StopRequested(BaseException):
    def __init__(self, number):
        self.number = number


class Stop:
    def __init__(self):
        self.active = True
        self.number = None

    def handler(self, number, frame):
        self.number = number
        if not self.active:
            raise StopRequested(number)


@contextmanager
def _signals(stop):
    old = {}
    if threading.current_thread() is threading.main_thread():
        for number in [signal.SIGINT, signal.SIGTERM]:
            old[number] = signal.signal(number, stop.handler)
    try:
        yield
    finally:
        for number, handler in old.items():
            signal.signal(number, handler)


def _commit(recorder, definition, status, op, output):
    root = recorder.save_checkpoint()
    result = dict(
        format="zlscan.recording-run/1",
        mode="physical_recording",
        origin="SIMULATED",
        visibility="ORACLE_ONLY",
        status=status,
        op=op,
        summary=recorder.summary(),
        checkpoint_sha256=sha256_file(root),
        definition_sha256=sha256_file(recorder.directory / "definition.json"),
    )
    publish_document(recorder.directory, "run.json", json.dumps(result, allow_nan=False).encode())
    output.write(json.dumps(result, allow_nan=False) + "\n")
    output.flush()


def _advance(recorder, count, definition, stop):
    while count and not stop.number:
        part = min(count, definition["chunk_steps"])
        recorder.advance(part)
        count -= part


def _dispatch(recorder, definition, command, stop):
    if not isinstance(command, dict):
        raise ValueError("command must be a JSON object")
    op = command.get("op")
    fields_by_op = {
        "advance": ({"op", "steps"}, set()),
        "lines": ({"op", "count"}, set()),
        "sync": ({"op", "ticks"}, set()),
        "request": (
            {"op", "channel", "parameter", "value", "effective_step"},
            {"readback_step", "readback_value"},
        ),
        "reset": ({"op"}, set()),
        "start": ({"op"}, set()),
        "checkpoint": ({"op"}, set()),
        "finish": ({"op"}, set()),
    }
    if op not in fields_by_op:
        raise ValueError("unknown recording operation")
    required, optional = fields_by_op[op]
    if not required <= set(command) or set(command) - required - optional:
        raise ValueError("missing or unknown command fields")
    pipeline = recorder.recording.pipeline

    def sync(ticks):
        if len(pipeline.acquisition.events) + len(ticks) > definition["max_pending_syncs"]:
            raise ValueError("pending sync software limit exceeded")
        recorder.schedule_syncs(ticks)

    if op == "advance":
        _advance(recorder, _positive(command["steps"]), definition, stop)
    elif op == "lines":
        count = _positive(command["count"])
        cfg = next(iter(recorder.base.values()))
        steps = line_steps(cfg)
        exact = cfg.scan.line_period_s * cfg.fs_hz * cfg.os
        if recorder.step % steps or not math.isclose(exact, steps, rel_tol=0, abs_tol=1e-8):
            raise ValueError("lines requires an aligned integer fine-step line period")
        for _ in range(count):
            if stop.number:
                break
            k = recorder.step // steps
            tick = round(
                float(
                    line_start_s(
                        cfg.scan,
                        np.array([k]),
                        next(iter(recorder.chains.values())).rng,
                        cfg.domain_id,
                    )[0]
                )
                * 10**12
            )
            sync([tick + cfg.scan.sync_offset_ps])
            _advance(recorder, steps, definition, stop)
    elif op == "sync":
        ticks = command["ticks"]
        if not isinstance(ticks, list):
            raise ValueError("sync ticks must be a list")
        sync([_nonnegative(t) for t in ticks])
    elif op in {"request", "reset", "start"}:
        reserved = 6 if op == "start" else 8  # Leave room for a reset/start pair and DONE.
        if len(recorder.controller.log) + reserved > definition["max_control_events"]:
            raise ValueError("control history software limit exceeded; no history discarded")
        if op == "request":
            readback = command.get("readback_step")
            recorder.request(
                command["channel"],
                command["parameter"],
                command["value"],
                effective_step=_nonnegative(command["effective_step"]),
                readback_step=_nonnegative(readback) if readback is not None else None,
                readback_value=command.get("readback_value"),
            )
        elif op == "reset":
            recorder.reset_control()
        else:
            recorder.start()
    elif op == "finish":
        recorder.finish()
    return op


def run_recording(
    config: Path | None,
    resume: Path | None,
    target: Path | None,
    commands: TextIO,
    output: TextIO,
    errors: TextIO,
) -> int:
    stop = Stop()
    try:
        with _signals(stop), ExitStack() as stack:
            if resume:
                source = resume.resolve()
                target = target.resolve() if target else source
                recorder, definition = _restore(source, target, stack)
            else:
                if config is None:
                    raise ValueError("new recording requires --config")
                target = (target or Path("recording_out")).resolve()
                recorder, definition = _create(target, config)
            stack.enter_context(recorder)
            _commit(
                recorder,
                definition,
                "complete" if recorder.recording.pipeline.closed else "committed",
                "open",
                output,
            )
            try:
                while not stop.number:
                    stop.active = False
                    line = commands.readline(MAX_COMMAND_BYTES + 1)
                    stop.active = True
                    if not line:
                        break
                    if len(line.encode()) > MAX_COMMAND_BYTES:
                        raise ValueError("recording command exceeds 65536 bytes")
                    op = _dispatch(recorder, definition, _json(line), stop)
                    status = (
                        "complete"
                        if recorder.recording.pipeline.closed
                        else ("paused" if stop.number else "committed")
                    )
                    _commit(recorder, definition, status, op, output)
                    if status == "complete" or stop.number:
                        return 128 + stop.number if stop.number else 0
            except StopRequested as exc:
                stop.active = True
                stop.number = exc.number
            _commit(
                recorder,
                definition,
                "complete" if recorder.recording.pipeline.closed else "paused",
                "interrupt" if stop.number else "eof",
                output,
            )
            return 128 + stop.number if stop.number else 0
    except (ValueError, OSError, TypeError, KeyError) as exc:
        errors.write(f"物理录制失败；保留上次提交根与原始文件：{exc}\n")
        errors.flush()
        return 1
