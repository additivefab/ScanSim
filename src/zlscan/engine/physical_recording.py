"""Own continuous physical channels, timed model controls and committed digital output."""

from __future__ import annotations

import json
import math
import os
import threading
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np

from zlscan.adc.patterns import ConvertedSamples
from zlscan.adc_link.link import LinkConfig
from zlscan.control.state import Controller, State, split_by_epoch
from zlscan.digital.acquisition import AcquisitionConfig
from zlscan.engine.chain import Chain, ChainConfig
from zlscan.engine.digital_pipeline import DigitalPipeline
from zlscan.engine.digital_recording import DigitalRecorder, _serialized
from zlscan.engine.validity import physical_applicability
from zlscan.io.committed import publish_checkpoint, publish_document
from zlscan.io.probe_store import MAX_CHUNK, TracePolicy, TraceWriter, validate_checkpoint
from zlscan.transport.queues import StageSpec


class PhysicalRecorder:
    FORMAT = "zlscan-physical-recording-checkpoint/1"
    TRACE_FORMAT = "zlscan-physical-recording-checkpoint/2"
    PARAMETERS = {"source.power_w", "detector.gain"}
    _hash = staticmethod(DigitalRecorder._hash)

    @staticmethod
    def _configs(configs: dict[int, ChainConfig], link: LinkConfig) -> dict[int, ChainConfig]:
        if not configs or any(type(c) is not int or not 0 <= c < 16 for c in configs):
            raise ValueError("physical recording needs explicit channels in 0..15")
        owned = deepcopy(dict(sorted(configs.items())))
        first = next(iter(owned.values()))

        def basis(c):
            return (c.fs_hz, c.os, c.domain_id, c.adc.bits, c.scan)

        if any(basis(c) != basis(first) for c in owned.values()) or first.adc.bits != link.bits:
            raise ValueError("physical channels require common scan timing and ADC sampling")
        if first.scan.row_jitter_s and len({c.seed for c in owned.values()}) > 1:
            raise ValueError(
                "common scan jitter requires a common seed; independent noise keys pending"
            )
        return owned

    def __init__(
        self,
        directory: Path,
        configs: dict[int, ChainConfig],
        link: LinkConfig,
        acquisition: AcquisitionConfig,
        stages: list[StageSpec],
        *,
        session_id: int,
        run_id: str,
        branch_id: str,
        trace_policy: TracePolicy | None = None,
    ) -> None:
        self.base = self._configs(configs, link)
        if not isinstance(run_id, str) or not isinstance(branch_id, str):
            raise ValueError("run/branch identity must be strings")
        self.base_hashes = {str(c): cfg.hash() for c, cfg in self.base.items()}
        self.chains = {c: Chain(replace(cfg)) for c, cfg in self.base.items()}
        first = next(iter(self.chains.values()))
        self.domain, self.fine_clock = first.adc_clock, first.fine_clock
        self.channels = tuple(self.base)
        self.identity = {"run_id": run_id, "branch_id": branch_id, "session_id": session_id}
        self.controller = Controller()
        self.commands: list[dict] = []
        self.applied_commands = 0
        self.failed = False
        self._mutex = threading.RLock()
        pipeline = DigitalPipeline(self.domain, link, acquisition, stages, self.channels)
        self.recording = DigitalRecorder(directory, pipeline, session_id)
        self.directory = self.recording.directory
        self.trace = None
        try:
            if trace_policy is not None:
                self.trace = TraceWriter(self.directory / "probes", trace_policy, self.channels)
                for chain in self.chains.values():
                    chain.probe_names = set(trace_policy.probes)
        except BaseException:
            self.recording.__exit__()
            raise
        self.start()

    @property
    def step(self) -> int:
        return next(iter(self.chains.values())).step

    def _healthy(self) -> None:
        if self.failed or self.recording.failed:
            raise OSError("physical recording has failed; restore a committed checkpoint")

    def _open(self) -> None:
        self._healthy()
        if self.recording.pipeline.closed:
            raise ValueError("physical input has finished")

    @_serialized
    def start(self) -> None:
        self._open()
        if self.controller.state is not State.UNCONFIGURED:
            raise ValueError("functional start requires unconfigured logical control")
        tick = self.fine_clock.tick_of(self.step)
        for state in (State.CONFIGURED, State.SETTLING, State.READY, State.SCANNING):
            self.controller.to(state, tick)

    @_serialized
    def schedule_syncs(self, ticks: list[int]) -> None:
        self._open()
        now = self.fine_clock.tick_of(self.step)
        if any(type(tick) is not int or tick < now for tick in ticks):
            raise ValueError("physical sync cannot rewrite an already advanced time")
        self.recording.schedule_syncs(ticks)

    @staticmethod
    def _validate_command(command: dict, channels: tuple[int, ...]) -> None:
        if (
            type(command["channel"]) is not int
            or command["channel"] not in channels
            or command["parameter"] not in PhysicalRecorder.PARAMETERS
        ):
            raise ValueError("unsupported physical channel or parameter command")
        value = command["value"]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError("physical command requires a finite numeric value")
        if value < 0 or command["parameter"] == "detector.gain" and value == 0:
            raise ValueError("power must be nonnegative and gain positive")

    @staticmethod
    def _apply_config(config: ChainConfig, command: dict) -> None:
        section, field = command["parameter"].split(".")
        setattr(config, section, replace(getattr(config, section), **{field: command["value"]}))

    def _apply_due(self) -> None:
        while self.applied_commands < len(self.commands):
            command = self.commands[self.applied_commands]
            if int(command["effective_step"]) > self.step:
                break
            self._apply_config(self.chains[command["channel"]].cfg, command)
            self.applied_commands += 1

    @_serialized
    def request(
        self,
        channel: int,
        parameter: str,
        value: float,
        *,
        effective_step: int,
        readback_step: int | None = None,
        readback_value=None,
    ) -> None:
        self._open()
        if self.controller.state is not State.SCANNING:
            raise ValueError("physical command requires functional scanning")
        command = {
            "channel": channel,
            "parameter": parameter,
            "value": value,
            "request_step": str(self.step),
            "effective_step": str(effective_step),
            "readback_step": str(readback_step) if readback_step is not None else None,
            "readback_value": Controller._copy_value(readback_value)
            if readback_step is not None
            else None,
        }
        self._validate_command(command, self.channels)
        if (
            type(effective_step) is not int
            or effective_step < self.step
            or (self.commands and effective_step < int(self.commands[-1]["effective_step"]))
        ):
            raise ValueError("command cannot rewrite already generated physical steps")
        if readback_step is not None and (
            type(readback_step) is not int or readback_step < effective_step
        ):
            raise ValueError("readback step must follow command effect")
        self.controller.request(
            f"channel.{channel}.{parameter}",
            value,
            self.fine_clock.tick_of(self.step),
            self.fine_clock.tick_of(effective_step),
            self.fine_clock.tick_of(readback_step) if readback_step is not None else None,
            readback_value,
        )
        self.commands.append(command)
        self._apply_due()

    @_serialized
    def reset_control(self) -> None:
        self._open()
        self.controller.reset(self.fine_clock.tick_of(self.step))

    @_serialized
    def advance(self, fine_steps: int) -> None:
        self._open()
        if type(fine_steps) is not int or fine_steps <= 0:
            raise ValueError("physical advance needs a positive integer step count")
        if self.controller.state is not State.SCANNING:
            raise ValueError("start logical control before advancing physical recording")
        end = self.step + fine_steps
        Controller._tick(self.fine_clock.tick_of(end))
        try:
            while self.step < end:
                self._apply_due()
                stop = min(end, self.step + MAX_CHUNK) if self.trace is not None else end
                if self.applied_commands < len(self.commands):
                    stop = min(stop, int(self.commands[self.applied_commands]["effective_step"]))
                count = stop - self.step
                start = self.step
                outputs = [self.chains[c].advance(count) for c in self.channels]
                if self.trace is not None:
                    self.trace.append(
                        start,
                        {c: out.probes for c, out in zip(self.channels, outputs, strict=True)},
                    )
                first = outputs[0]
                if any(
                    o.first_sample != first.first_sample or len(o.codes) != len(first.codes)
                    for o in outputs
                ):
                    raise ValueError("physical channels produced different sample boundaries")
                if len(first.codes):
                    source = ConvertedSamples(
                        np.column_stack([o.codes for o in outputs]),
                        self.channels,
                        first.first_sample,
                        0,
                        self.domain,
                        self.recording.pipeline.link_config.bits,
                        **self.identity,
                    )
                    for part in split_by_epoch(source, self.controller):
                        self.recording.push(part)
            self._apply_due()
        except Exception:
            self.failed = True
            raise

    @_serialized
    def finish(self) -> None:
        self._healthy()
        if self.recording.pipeline.closed:
            return
        if self.applied_commands != len(self.commands):
            raise ValueError(
                "future physical commands remain; advance to their effect before finishing"
            )
        if self.controller.state is not State.SCANNING:
            raise ValueError("finish requires functional scanning")
        try:
            tick = self.fine_clock.tick_of(self.step)
            self.controller.to(State.DRAINING, tick)
            self.recording.finish()
            self.controller.to(State.DONE, max(tick, self.recording.pipeline.watermark or tick))
        except Exception:
            self.failed = True
            raise

    def _physical_consistent(self) -> None:
        if {str(c): cfg.hash() for c, cfg in self.base.items()} != self.base_hashes:
            raise ValueError("base physical configuration changed")
        if any(chain.step != self.step for chain in self.chains.values()):
            raise ValueError("physical channel step boundaries disagree")
        expected_applied = sum(int(c["effective_step"]) <= self.step for c in self.commands)
        if self.applied_commands != expected_applied:
            raise ValueError("physical command cursor differs from the fine-step boundary")
        expected = {c: replace(cfg) for c, cfg in self.base.items()}
        command_rows = [
            row for row in self.controller.log if row.kind in {"request", "effective", "readback"}
        ]
        wanted_rows = []
        for i, command in enumerate(self.commands):
            self._validate_command(command, self.channels)
            key = f"channel.{command['channel']}.{command['parameter']}"
            for kind, step_name, val in (
                ("request", "request_step", command["value"]),
                ("effective", "effective_step", command["value"]),
                ("readback", "readback_step", command["readback_value"]),
            ):
                if command[step_name] is not None:
                    wanted_rows.append(
                        {
                            "kind": kind,
                            "tick": self.fine_clock.tick_of(int(command[step_name])),
                            "detail": {"key": key, "value": val, "command_id": str(i)},
                        }
                    )
            if i < self.applied_commands:
                self._apply_config(expected[command["channel"]], command)

        if wanted_rows != [asdict(row) for row in command_rows]:
            raise ValueError("physical command plan differs from the control history")
        if any(chain.cfg.hash() != expected[c].hash() for c, chain in self.chains.items()):
            raise ValueError("effective physical configuration differs from applied commands")

    def _consistent(self) -> None:
        self._physical_consistent()
        self._pipeline_consistent(self.recording.pipeline, self.recording.next_source_index)
        if self.trace is not None and self.trace.step != self.step:
            raise ValueError("physical and probe committed boundaries disagree")

    def _pipeline_consistent(
        self, pipeline: DigitalPipeline, next_source_index: int | None
    ) -> None:
        next_source = -(-self.step // next(iter(self.base.values())).os)
        if next_source_index != (next_source if self.step else None):
            raise ValueError("physical and digital source sample boundaries disagree")
        identity = pipeline.link.identity
        if identity is not None and identity != (
            self.identity["run_id"],
            self.identity["branch_id"],
            self.identity["session_id"],
        ):
            raise ValueError("physical and digital sample identity differs")
        if pipeline.closed != (self.controller.state is State.DONE):
            raise ValueError("physical controller and digital completion state differ")

    @_serialized
    def checkpoint(self) -> bytes:
        self._healthy()
        try:
            self._consistent()
            value = {
                "format": self.TRACE_FORMAT if self.trace is not None else self.FORMAT,
                "origin": "SIMULATED",
                "visibility": "ORACLE_ONLY",
                "base_hashes": self.base_hashes,
                "identity": self.identity,
                "chains": {
                    str(c): {**json.loads(chain.checkpoint()), "step": str(chain.step)}
                    for c, chain in self.chains.items()
                },
                "control": json.loads(self.controller.checkpoint()),
                "commands": self.commands,
                "applied_commands": str(self.applied_commands),
                "recording": json.loads(self.recording.checkpoint()),
            }
            if self.trace is not None:
                value["trace"] = self.trace.checkpoint()
            value["checksum"] = self._hash(value)
            return json.dumps(value, sort_keys=True, allow_nan=False).encode()
        except Exception:
            self.failed = True
            raise

    @_serialized
    def save_checkpoint(self) -> Path:
        data = self.checkpoint()
        try:
            root = publish_checkpoint(self.directory, data)
            history = self.directory / "history"
            new_directory = not history.exists()
            history.mkdir(exist_ok=True)
            name = f"{self.step:020d}-{self._hash_bytes(data)}.json"
            archived = history / name
            if archived.exists():
                if archived.read_bytes() != data:
                    raise ValueError("immutable physical history checkpoint differs")
            else:
                publish_document(history, name, data)
            if new_directory and os.name != "nt":
                fd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            return root
        except Exception:
            self.failed = True
            raise

    @staticmethod
    def _hash_bytes(data: bytes) -> str:
        import hashlib

        return hashlib.sha256(data).hexdigest()

    @classmethod
    def restore(
        cls,
        directory: Path,
        configs: dict[int, ChainConfig],
        link: LinkConfig,
        acquisition: AcquisitionConfig,
        stages: list[StageSpec],
        blob: bytes,
        *,
        branch_directory: Path | None = None,
    ) -> PhysicalRecorder:
        result = cls._restore_physics(configs, link, blob)
        value = json.loads(blob)
        result.recording = DigitalRecorder.restore(
            directory,
            result.domain,
            link,
            acquisition,
            stages,
            result.channels,
            json.dumps(value["recording"]).encode(),
            branch_directory=branch_directory,
        )
        result.directory = result.recording.directory
        try:
            if "trace" in value:
                result.trace = TraceWriter.restore(
                    directory / "probes",
                    value["trace"],
                    branch_directory=result.directory / "probes"
                    if branch_directory is not None
                    else None,
                )
                if result.trace.channels != result.channels:
                    raise ValueError("physical and probe channels differ")
                for chain in result.chains.values():
                    chain.probe_names = set(result.trace.policy.probes)
            result._consistent()
        except Exception:
            result.__exit__()
            raise
        return result

    @classmethod
    def _restore_physics(
        cls, configs: dict[int, ChainConfig], link: LinkConfig, blob: bytes
    ) -> PhysicalRecorder:
        """Validate/rebuild only model state; do not open or resume any output writer."""
        value = json.loads(blob)
        if not isinstance(value, dict) or value.get("format") not in {cls.FORMAT, cls.TRACE_FORMAT}:
            raise ValueError("physical recording checkpoint version is unsupported")
        if ("trace" in value) != (value["format"] == cls.TRACE_FORMAT):
            raise ValueError("probe checkpoint requires the version 2 recovery contract")
        if value.pop("checksum", None) != cls._hash(value):
            raise ValueError("physical recording checkpoint checksum differs")
        if value.get("origin") != "SIMULATED" or value.get("visibility") != "ORACLE_ONLY":
            raise ValueError("physical checkpoint provenance differs")
        result = cls.__new__(cls)
        result.base = cls._configs(configs, link)
        result.base_hashes = {str(c): cfg.hash() for c, cfg in result.base.items()}
        if result.base_hashes != value["base_hashes"]:
            raise ValueError("base physical configuration differs")
        result.channels = tuple(result.base)
        result.identity, result.commands = value["identity"], value["commands"]
        result.applied_commands = int(value["applied_commands"])
        result.chains = {c: Chain(replace(cfg)) for c, cfg in result.base.items()}
        for command in result.commands[: result.applied_commands]:
            cls._validate_command(command, result.channels)
            cls._apply_config(result.chains[command["channel"]].cfg, command)
        for c, chain in result.chains.items():
            snapshot = {**value["chains"][str(c)], "step": int(value["chains"][str(c)]["step"])}
            chain.restore(json.dumps(snapshot).encode())
        result.controller = Controller.restore(json.dumps(value["control"]).encode())
        first = next(iter(result.chains.values()))
        result.domain, result.fine_clock = first.adc_clock, first.fine_clock
        result._mutex, result.failed = threading.RLock(), False
        result.trace = None
        if "trace" in value:
            trace, _, channels = validate_checkpoint(value["trace"])
            if channels != result.channels or int(trace["step"]) != result.step:
                raise ValueError("physical and probe checkpoint identities differ")
        result._physical_consistent()
        return result

    @_serialized
    def summary(self) -> dict:
        return {
            **self.recording.summary(),
            "fine_step": str(self.step),
            "control_state": self.controller.state.value,
            "applied_commands": self.applied_commands,
            "failed": self.failed or self.recording.failed,
            "physical_applicability": [
                physical_applicability(chain.cfg) for chain in self.chains.values()
            ],
        }

    def __enter__(self) -> PhysicalRecorder:
        return self

    @_serialized
    def __exit__(self, *args) -> None:
        if self.trace is not None:
            self.trace.__exit__()
        self.recording.__exit__()
