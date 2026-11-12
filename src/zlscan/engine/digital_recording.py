"""Pair digital state with committed raw and ORACLE_ONLY diagnostic file cursors."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict
from functools import wraps
from pathlib import Path

from zlscan.adc.patterns import ConvertedSamples
from zlscan.adc_link.link import LinkConfig
from zlscan.contracts.time import ClockDomain
from zlscan.digital.acquisition import AcquisitionConfig, channel_mask
from zlscan.engine.digital_pipeline import DigitalPipeline
from zlscan.io.committed import CommittedFile, publish_checkpoint
from zlscan.io.observable_arrays import encode_arrays
from zlscan.io.rawstream import FLAG_EPOCH_START, RawWriter
from zlscan.transport.queues import StageSpec


def _serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._mutex:
            return method(self, *args, **kwargs)

    return call


class DigitalRecorder:
    FORMAT = "zlscan-recording-checkpoint/1"

    def __init__(self, directory: Path, pipeline: DigitalPipeline, session_id: int) -> None:
        if pipeline.link.next_index is not None or pipeline.closed:
            raise ValueError("recorder must own the pipeline before source input starts")
        self._mutex = threading.RLock()
        self.directory, self.pipeline = Path(directory).resolve(), pipeline
        self.session_id = session_id
        header = pipeline.session_header(session_id)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.writer = RawWriter(self.directory / "raw.zlraw", header)
        self.journal = None
        try:
            self.journal = CommittedFile(self.directory / "events.jsonl")
            self._journal(
                {
                    "format": "zlscan-digital-journal/1",
                    "origin": "SIMULATED",
                    "visibility": "ORACLE_ONLY",
                    "purpose": "simulation diagnostics; not calibration or reconstruction input",
                    "session": header.model_dump(mode="json"),
                    "time_semantics": "last declared transport stage completion; not host arrival",
                }
            )
        except Exception:
            self.writer.__exit__()
            if self.journal is not None:
                self.journal.close()
            raise
        self.failed = False
        self.last_epoch: int | None = None
        self.last_packet_id: int | None = None
        self.records = self.delivered = self.lost = self.retained_samples = 0

    @property
    @_serialized
    def next_source_index(self) -> int | None:
        return self.pipeline.link.next_index

    def _healthy(self) -> None:
        if self.failed:
            raise OSError("recording has failed; restore a committed paired checkpoint")

    def _journal(self, value: dict) -> None:
        self.journal.append(
            (
                json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
            ).encode()
        )

    def _consume(self) -> None:
        batch = self.pipeline.drain()
        entries = []
        for delivered in batch.delivered:
            packet, source = delivered.packet, delivered.packet.source
            if self.last_packet_id is not None and packet.item.item_id <= self.last_packet_id:
                raise ValueError("delivery would repeat or regress a committed packet")
            sequence, offset = self.writer.next_sequence, self.writer.offset
            flags = packet.flags | (FLAG_EPOCH_START if source.epoch != self.last_epoch else 0)
            self.writer.write_block(
                epoch=source.epoch,
                channel_mask=channel_mask(source.channels),
                clock_domain=source.domain.id,
                first_sample_index=source.first_index,
                samples=source.codes,
                flags=flags,
                line_number=packet.item.line_number,
            )
            entries.append(
                {
                    "packet_id": str(packet.item.item_id),
                    "block_sequence": str(sequence),
                    "raw_offset": str(offset),
                    "raw_end": str(self.writer.offset),
                    "delivery_tick": str(delivered.delivery_tick),
                    "flags": flags,
                    "line_number": str(packet.item.line_number),
                    "window": packet.window,
                    "source": {
                        "run_id": source.run_id,
                        "branch_id": source.branch_id,
                        "session_id": source.session_id,
                        "epoch": source.epoch,
                        "clock_domain": source.domain.id,
                        "channels": list(source.channels),
                        "first_sample_index": str(source.first_index),
                        "count": len(source),
                        "observables": encode_arrays(source.meta),
                    },
                }
            )
            self.last_epoch, self.last_packet_id = source.epoch, packet.item.item_id
            self.delivered += 1
            self.retained_samples += len(source)
        losses = []
        for loss in batch.losses:
            value = asdict(loss)
            for key in ("first_sample_index", "last_sample_index", "time_ps", "item_id"):
                value[key] = str(value[key])
            losses.append(value)
        stats = [
            {
                "name": stat.name,
                "peak_bytes": stat.peak_bytes,
                "max_wait_ps": str(stat.max_wait_ps),
                "overflow_count": stat.overflow_count,
                "occupancy": [[str(tick), count] for tick, count in stat.occupancy],
            }
            for stat in batch.stats
        ]
        if (
            entries
            or losses
            or batch.ignored_syncs
            or batch.skipped_lines
            or any(s.occupancy for s in batch.stats)
        ):
            self._journal(
                {
                    "kind": "producer_batch",
                    "origin": "SIMULATED",
                    "visibility": "ORACLE_ONLY",
                    "watermark": str(self.pipeline.watermark),
                    "delivered": entries,
                    "losses": losses,
                    "stats": stats,
                    "ignored_syncs": [str(t) for t in batch.ignored_syncs],
                    "skipped_lines": [[str(n), reason] for n, reason in batch.skipped_lines],
                }
            )
            self.records += 1
        self.lost += len(losses)

    @_serialized
    def schedule_syncs(self, ticks: list[int]) -> None:
        self._healthy()
        self.pipeline.schedule_syncs(ticks)

    @_serialized
    def push(self, source: ConvertedSamples) -> None:
        self._healthy()
        if source.session_id != self.session_id:
            raise ValueError("recording session differs from source identity")
        try:
            self.pipeline.push(source)
            self._consume()
        except Exception:
            self.failed = True
            raise

    @_serialized
    def finish(self) -> None:
        self._healthy()
        try:
            self.pipeline.finish()
            self._consume()
        except Exception:
            self.failed = True
            raise

    @_serialized
    def summary(self) -> dict:
        return {
            "written_blocks": self.delivered,
            "lost_blocks": self.lost,
            "retained_samples_per_channel": self.retained_samples,
            "closed": self.pipeline.closed,
            "failed": self.failed,
        }

    @staticmethod
    def _hash(value: dict) -> str:
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, allow_nan=False).encode()
        ).hexdigest()

    @_serialized
    def checkpoint(self) -> bytes:
        self._healthy()
        try:
            value = {
                "format": self.FORMAT,
                "session_id": self.session_id,
                "pipeline": json.loads(self.pipeline.checkpoint()),
                "writer": json.loads(self.writer.checkpoint()),
                "journal": self.journal.checkpoint(),
                "last_epoch": self.last_epoch,
                "last_packet_id": str(self.last_packet_id)
                if self.last_packet_id is not None
                else None,
                "records": str(self.records),
                "delivered": str(self.delivered),
                "lost": str(self.lost),
                "retained_samples": str(self.retained_samples),
            }
            value["checksum"] = self._hash(value)
            return json.dumps(value, sort_keys=True, allow_nan=False).encode()
        except Exception:
            self.failed = True
            raise

    @_serialized
    def save_checkpoint(self) -> Path:
        data = self.checkpoint()
        try:
            return publish_checkpoint(self.directory, data)
        except Exception:
            self.failed = True
            raise

    @classmethod
    def restore(
        cls,
        directory: Path,
        domain: ClockDomain,
        link: LinkConfig,
        acquisition: AcquisitionConfig,
        stages: list[StageSpec],
        channels: tuple[int, ...],
        blob: bytes,
        *,
        branch_directory: Path | None = None,
    ) -> DigitalRecorder:
        value = json.loads(blob)
        if not isinstance(value, dict) or value.get("format") != cls.FORMAT:
            raise ValueError("recording checkpoint version is unsupported")
        if value.pop("checksum", None) != cls._hash(value):
            raise ValueError("recording checkpoint checksum differs")
        pipeline = DigitalPipeline.restore(
            domain, link, acquisition, stages, channels, json.dumps(value["pipeline"]).encode()
        )
        source = Path(directory).resolve()
        target = Path(branch_directory).resolve() if branch_directory else source
        if branch_directory:
            target.mkdir(parents=True, exist_ok=False)
        writer = RawWriter.resume(
            source / "raw.zlraw",
            pipeline.session_header(value["session_id"]),
            json.dumps(value["writer"]).encode(),
            branch_path=target / "raw.zlraw" if branch_directory else None,
        )
        try:
            journal = CommittedFile.resume(
                source / "events.jsonl",
                value["journal"],
                branch_path=target / "events.jsonl" if branch_directory else None,
            )
        except Exception:
            writer.__exit__()
            raise
        result = cls.__new__(cls)
        result._mutex = threading.RLock()
        result.directory, result.pipeline, result.writer, result.journal = (
            target,
            pipeline,
            writer,
            journal,
        )
        result.session_id, result.last_epoch = value["session_id"], value["last_epoch"]
        result.last_packet_id = (
            int(value["last_packet_id"]) if value["last_packet_id"] is not None else None
        )
        for key in ("records", "delivered", "lost", "retained_samples"):
            setattr(result, key, int(value[key]))
        result.failed = False
        try:
            result._verify_journal()
        except Exception:
            result.__exit__()
            raise
        return result

    def _verify_journal(self) -> None:
        stream = self.journal.stream
        stream.seek(0)
        first = stream.readline()
        header = json.loads(first)
        if (
            not first.endswith(b"\n")
            or header.get("format") != "zlscan-digital-journal/1"
            or header.get("origin") != "SIMULATED"
            or header.get("visibility") != "ORACLE_ONLY"
            or header.get("session") != self.writer.session.model_dump(mode="json")
        ):
            raise ValueError("committed diagnostic journal header differs")
        rows, last = 1, first[-1:]
        while data := stream.read(1 << 20):
            rows += data.count(b"\n")
            last = data[-1:]
        if last != b"\n" or rows != self.records + 1:
            raise ValueError("committed diagnostic journal boundary or record count differs")
        if self.writer.next_sequence != self.delivered:
            raise ValueError("committed raw block count differs from recording summary")
        stream.seek(self.journal.offset)
        self.journal.check()

    def __enter__(self) -> DigitalRecorder:
        return self

    @_serialized
    def __exit__(self, *args) -> None:
        try:
            self.writer.__exit__()
        finally:
            self.journal.close()
