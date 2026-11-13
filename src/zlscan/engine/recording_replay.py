"""Read-only multi-channel replay of committed physical recording history."""

from __future__ import annotations

import hashlib
import json
import re
import time
from contextlib import ExitStack
from pathlib import Path

import numpy as np

from zlscan.engine.chain import PROBE_NAMES
from zlscan.engine.digital_pipeline import DigitalPipeline
from zlscan.engine.physical_recording import PhysicalRecorder
from zlscan.engine.recording_cli import read_definition
from zlscan.io.committed import verify_committed_prefix as _prefix
from zlscan.io.manifest import sha256_file
from zlscan.io.model_quality import QualityReader
from zlscan.io.probe_store import TraceReader
from zlscan.io.producer_history import HistoryReader
from zlscan.io.rawstream import RawWriter, verify_raw_prefix

MAX_WINDOW_STEPS = 1_000_000
_HISTORY = re.compile(r"([0-9]+)-([0-9a-f]{64})\.json")


def _checksum(value, expected):
    if value.get("format") != expected:
        raise ValueError("recording component version differs")
    body = {k: v for k, v in value.items() if k != "checksum"}
    if value.get("checksum") != PhysicalRecorder._hash(body):
        raise ValueError("recording component checksum differs")


class RecordingReplay:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.stack = ExitStack()
        try:
            (
                self.definition,
                self.configs,
                self.link,
                self.acquisition,
                self.stages,
                self.spec,
                definition_bytes,
            ) = read_definition(self.directory, self.stack)
            self.root_blob = (self.directory / "checkpoint.json").read_bytes()
            self.root = json.loads(self.root_blob)
            self.current = self._decode(self.root_blob)
            self.step = self.current.step
            self.record = self.root["recording"]
            self.history_reader = None
            if "digital_history" in self.root:
                self.history_reader = self.stack.enter_context(
                    HistoryReader(
                        self.directory / "producer_history.jsonl", self.root["digital_history"]
                    )
                )
            if (self.history_reader is not None) != self.spec.record_digital_history:
                raise ValueError("producer history policy differs from saved input")
            self.quality_reader = None
            if "quality" in self.root:
                self.quality_reader = self.stack.enter_context(
                    QualityReader(self.directory / "quality_model.jsonl", self.root["quality"])
                )
            if self.quality_reader is not None:
                self.quality_reader.check_epochs(
                    lambda step: (
                        self.current.controller.epoch_at(self.current.fine_clock.tick_of(step)).id
                    )
                )
            if (self.quality_reader is not None) != self.spec.record_quality:
                raise ValueError("saved model quality policy differs from recording input")
            self.trace_reader = None
            if "trace" in self.root:
                self.trace_reader = self.stack.enter_context(
                    TraceReader(self.directory / "probes", self.root["trace"])
                )
            if (
                self.trace_reader.policy if self.trace_reader is not None else None
            ) != self.spec.trace:
                raise ValueError("saved probe policy differs from original recording input")
            self.raw_cursor = self.record["writer"]["file"]
            self.journal_cursor = self.record["journal"]
            digest = hashlib.sha256(definition_bytes).hexdigest()
            self.input_hashes = {
                self.directory / "definition.json": digest,
                self.directory / "definition.sha256": hashlib.sha256(digest.encode()).hexdigest(),
                self.directory / "inputs/recording.json": self.definition["input_sha256"],
            }
            for row in self.definition["channels"]:
                directory = self.directory / row["path"]
                body = (directory / "config.json").read_bytes()
                if hashlib.sha256(body).hexdigest() != row["config_sha256"]:
                    raise ValueError("recording configuration changed during registration")
                self.input_hashes[directory / "config.json"] = row["config_sha256"]
                for i, scene in enumerate(json.loads(body)["config"]["scenes"]):
                    self.input_hashes[directory / f"scene_{i}.npy"] = scene["sha256"]
            self.tail = self.validate_prefixes()
            self._file_headers()
        except BaseException:
            self.stack.close()
            raise

    def _decode(self, blob):
        state = json.loads(blob)
        result = PhysicalRecorder._restore_physics(self.configs, self.link, blob)
        if result.identity != dict(
            session_id=self.spec.session_id, run_id=self.spec.run_id, branch_id=self.spec.branch_id
        ):
            raise ValueError("recording replay source identity differs")
        record = state["recording"]
        _checksum(record, "zlscan-recording-checkpoint/1")
        _checksum(record["writer"], RawWriter.FORMAT)
        if record["session_id"] != result.identity["session_id"]:
            raise ValueError("recording and physical sessions differ")
        if int(record["writer"]["next_sequence"]) != int(record["delivered"]):
            raise ValueError("recording delivery and raw sequence differ")
        pipeline = DigitalPipeline.restore(
            result.domain,
            self.link,
            self.acquisition,
            self.stages,
            result.channels,
            json.dumps(record["pipeline"]).encode(),
        )
        result._pipeline_consistent(pipeline, pipeline.link.next_index)
        if "digital_history" in state:
            history = state["digital_history"]
            if (
                history["header"] != result.digital_history_header(pipeline)
                or history["after"] != result._hash_bytes(pipeline.checkpoint())
                or history["closed"] != pipeline.closed
            ):
                raise ValueError("producer history runtime state differs")
        header = pipeline.session_header(record["session_id"])
        if RawWriter._hash_session(header) != record["writer"]["session_hash"]:
            raise ValueError("recording session hash differs from runtime")
        return result

    def validate_prefixes(self):
        if any(sha256_file(path) != digest for path, digest in self.input_hashes.items()):
            raise ValueError("immutable recording input changed after registration")
        raw_tail = _prefix(self.directory / "raw.zlraw", self.raw_cursor)
        journal_tail = _prefix(self.directory / "events.jsonl", self.journal_cursor)
        trace_tail = self.trace_reader.unchanged() if self.trace_reader is not None else False
        quality_tail = self.quality_reader.unchanged() if self.quality_reader is not None else False
        history_tail = self.history_reader.unchanged() if self.history_reader is not None else False
        return raw_tail or journal_tail or trace_tail or quality_tail or history_tail

    def _file_headers(self, record=None):
        record = self.record if record is None else record
        raw_cursor = record["writer"]["file"]
        journal_cursor = record["journal"]
        with (self.directory / "raw.zlraw").open("rb") as stream:
            header = verify_raw_prefix(
                stream,
                int(raw_cursor["offset"]),
                record["writer"]["session_hash"],
                int(record["writer"]["next_sequence"]),
            )
        with (self.directory / "events.jsonl").open("rb") as stream:
            limit = int(journal_cursor["offset"])
            first = stream.readline(min(limit, 65536))
            if not first.endswith(b"\n"):
                raise ValueError("recording journal header missing")
            document = json.loads(first)
            if (
                document.get("format") != "zlscan-digital-journal/1"
                or document.get("origin") != "SIMULATED"
                or document.get("visibility") != "ORACLE_ONLY"
                or document.get("session") != header.model_dump(mode="json")
            ):
                raise ValueError("recording diagnostic journal provenance differs")
            rows = 1
            remaining = limit - len(first)
            last = first[-1:]
            while remaining:
                data = stream.read(min(1 << 20, remaining))
                if not data:
                    raise ValueError("recording journal prefix truncated")
                rows += data.count(b"\n")
                remaining -= len(data)
                last = data[-1:]
            if last != b"\n" or rows != int(record["records"]) + 1:
                raise ValueError("recording journal committed row count differs")

    def _nearest(self, start, *, need_history=False):
        best = None
        for path in (self.directory / "history").glob("*.json"):
            match = _HISTORY.fullmatch(path.name)
            if match is None:
                raise ValueError("unsupported recording history filename")
            point = int(match[1])
            if point > start:
                continue
            blob = path.read_bytes()
            if hashlib.sha256(blob).hexdigest() != match[2]:
                raise ValueError("recording history SHA mismatch")
            state = json.loads(blob)
            if need_history and (
                "digital_history" not in state or state["digital_history"]["closed"]
            ):
                continue
            channels = state.get("chains", {})
            if not channels or any(int(c["step"]) != point for c in channels.values()):
                raise ValueError("recording history step differs from its index")
            # Only roots in this committed command/control lineage may seed the replay.
            commands = state["commands"]
            log = state["control"]["log"]
            if (
                commands != self.root["commands"][: len(commands)]
                or log != self.root["control"]["log"][: len(log)]
            ):
                raise ValueError("recording history is not a prefix of the committed lineage")
            key = (
                point,
                int(state.get("digital_history", {}).get("records", 0)),
                len(commands),
                len(log),
                match[2],
            )
            if best is None or key > best[0]:
                best = (key, blob, path.name)
        if best is None:
            raise ValueError("no recorded checkpoint before the replay window")
        restored = self._decode(best[1])
        record = json.loads(best[1])["recording"]
        if int(record["writer"]["file"]["offset"]) > int(self.raw_cursor["offset"]) or int(
            record["journal"]["offset"]
        ) > int(self.journal_cursor["offset"]):
            raise ValueError("historical cursor exceeds current committed prefix")
        _prefix(self.directory / "raw.zlraw", record["writer"]["file"])
        _prefix(self.directory / "events.jsonl", record["journal"])
        state = json.loads(best[1])
        if "digital_history" in state:
            if self.history_reader is None:
                raise ValueError("historical producer cursor lacks a current history")
            # Validate an actual committed prefix, including operation count and boundary.
            HistoryReader(self.directory / "producer_history.jsonl", state["digital_history"])
        self._file_headers(record)
        return restored, best[2]

    def digital_state(self, tick):
        from zlscan.engine.digital_state import replay_state

        return replay_state(self, tick)

    def replay(self, start: int, steps: int, probes: set[str]):
        if (
            type(start) is not int
            or type(steps) is not int
            or start < 0
            or steps < 1
            or steps > MAX_WINDOW_STEPS
            or start + steps > self.step
        ):
            raise ValueError("recording replay window outside committed range or query limit")
        if probes - set(PROBE_NAMES):
            raise ValueError("unknown physical replay probe")
        begun = time.perf_counter()
        self.validate_prefixes()
        restored, name = self._nearest(start)
        base = restored.step
        cursor = restored.applied_commands
        plan = self.current.commands
        chains = restored.chains
        channels = list(restored.channels)
        end = start + steps
        first = -(-start // next(iter(chains.values())).cfg.os)
        samples = -(-end // next(iter(chains.values())).cfg.os) - first
        result = {"codes": np.empty((samples, len(channels)), dtype=np.uint16)}
        flags = ["saturated", "front_end_clipped", "over_limit"]
        for flag in flags:
            result[flag] = np.empty((samples, len(channels)), dtype=bool)
        for probe in probes:
            result[probe] = np.empty(
                (steps, len(channels)), dtype=np.int64 if probe in {"line", "facet"} else float
            )
        while restored.step < end:
            while cursor < len(plan) and int(plan[cursor]["effective_step"]) <= restored.step:
                command = plan[cursor]
                PhysicalRecorder._apply_config(chains[command["channel"]].cfg, command)
                cursor += 1
            stop = min(end, restored.step + self.definition["chunk_steps"])
            if restored.step < start:
                stop = min(stop, start)
            if cursor < len(plan):
                stop = min(stop, int(plan[cursor]["effective_step"]))
            count = stop - restored.step
            publish = restored.step >= start
            fine_offset = restored.step - start
            for column, channel in enumerate(channels):
                chain = chains[channel]
                chain.probe_names = probes if publish else set()
                out = chain.advance(count)
                if publish:
                    offset = out.first_sample - first
                    span = slice(offset, offset + len(out.codes))
                    result["codes"][span, column] = out.codes
                    for flag in flags:
                        result[flag][span, column] = getattr(out, flag)
                    for probe in probes:
                        result[probe][fine_offset : fine_offset + count, column] = out.probes[probe]
        while cursor < len(plan) and int(plan[cursor]["effective_step"]) <= end:
            command = plan[cursor]
            PhysicalRecorder._apply_config(chains[command["channel"]].cfg, command)
            cursor += 1
        epochs = np.array(
            [
                self.current.controller.epoch_at(self.current.domain.tick_of(i)).id
                for i in range(first, first + samples)
            ],
            dtype=np.uint32,
        )
        result["epochs"] = epochs
        result["final_states"] = {str(c): chains[c].checkpoint().decode() for c in channels}
        tail = self.validate_prefixes()
        if sha256_file(self.directory / "history" / name) != name[:-5].split("-")[1]:
            raise ValueError("recording history changed during replay")
        result["metadata"] = dict(
            mode="recording_replay",
            status="replayed",
            visibility="ORACLE_ONLY",
            origin="SIMULATED",
            source=str(self.directory),
            start_step=start,
            steps=steps,
            checkpoint_step=base,
            checkpoint_file=name,
            first_sample_index=first,
            channels=channels,
            discarded_steps=start - base,
            committed_fine_step=str(self.step),
            uncommitted_tail_present=tail,
            source_root_sha256=hashlib.sha256(self.root_blob).hexdigest(),
            wall_s=time.perf_counter() - begun,
            semantics="recomputed physical model; not delivered raw or measured codes",
        )
        return result

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stack.close()


def replay_recording(source: Path, output: Path, start: int, steps: int, probes: set[str]):
    if output.resolve().is_relative_to(source.resolve()) or output.exists():
        raise ValueError("recording replay requires a new output outside the source")
    with RecordingReplay(source) as saved:
        result = saved.replay(start, steps, probes)
        output.mkdir(parents=True, exist_ok=False)
        np.savez(
            output / "samples.npz",
            **{k: v for k, v in result.items() if k not in {"metadata", "final_states"}},
        )
        (output / "final_states.json").write_text(
            json.dumps(result["final_states"], indent=2) + "\n", encoding="utf-8"
        )
        (output / "run.json").write_text(
            json.dumps(result["metadata"], indent=2) + "\n", encoding="utf-8"
        )
