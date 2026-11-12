"""Compose functional ADC output, capture windows and transport with recoverable code payloads.

Checkpoints cover this digital pipeline at a producer time boundary. They do not yet cover
physical-chain/filter state, control commands or the external writer's committed file offset.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

import numpy as np

from zlscan.adc.patterns import ConvertedSamples
from zlscan.adc_link.link import LinkConfig, receive
from zlscan.adc_link.stream import StreamLink
from zlscan.contracts.time import CaptureClock, ClockDomain
from zlscan.digital.acquisition import AcquisitionConfig, session_header
from zlscan.digital.stream import CapturedWindow, StreamAcquisition
from zlscan.io.rawstream import FLAG_LINE_END, FLAG_LINE_START, SessionHeader
from zlscan.transport.queues import Item, StageSpec, StageStats, TransportLoss, TransportSimulation

INTEGER_META = {"capture_time", "capture_count", "selection_time", "window_count"}


@dataclass
class CapturedPacket:
    item: Item
    source: ConvertedSamples
    flags: int
    window: dict

    def to_dict(self) -> dict:
        return {
            "item": asdict(self.item),
            "flags": self.flags,
            "window": self.window,
            "source": {
                "codes": self.source.codes.tolist(),
                "first_index": str(self.source.first_index),
                "epoch": self.source.epoch,
                "bits": self.source.bits,
                "run_id": self.source.run_id,
                "branch_id": self.source.branch_id,
                "session_id": self.source.session_id,
                "meta": {
                    key: [str(int(v)) for v in value] if key in INTEGER_META else value.tolist()
                    for key, value in self.source.meta.items()
                },
            },
        }

    @classmethod
    def from_dict(
        cls, value: dict, domain: ClockDomain, channels: tuple[int, ...]
    ) -> CapturedPacket:
        source = value["source"]
        src = ConvertedSamples(
            np.asarray(source["codes"], np.uint16).reshape(-1, len(channels)),
            channels,
            int(source["first_index"]),
            source["epoch"],
            domain,
            source["bits"],
            source["run_id"],
            source["branch_id"],
            source["session_id"],
            {
                key: np.asarray(
                    [int(v) for v in data],
                    object if key in {"capture_count", "window_count"} else np.int64,
                )
                if key in INTEGER_META
                else np.asarray(data, bool)
                for key, data in source["meta"].items()
            },
        )
        return cls(Item(**value["item"]), src, value["flags"], value["window"])


@dataclass
class DeliveredPacket:
    packet: CapturedPacket
    delivery_tick: (
        int  # completion at the declared last transport stage, not necessarily host arrival
    )


@dataclass
class DigitalBatch:
    delivered: list[DeliveredPacket] = field(default_factory=list)
    losses: list[TransportLoss] = field(default_factory=list)
    ignored_syncs: list[int] = field(default_factory=list)
    skipped_lines: list[tuple[int, str]] = field(default_factory=list)
    stats: list[StageStats] = field(default_factory=list)


class DigitalPipeline:
    FORMAT = "zlscan-digital-checkpoint/1"

    def __init__(
        self,
        domain: ClockDomain,
        link: LinkConfig,
        acquisition: AcquisitionConfig,
        stages: list[StageSpec],
        channels: tuple[int, ...],
    ) -> None:
        if tuple(sorted(channels)) != channels:
            raise ValueError("digital pipeline source channels must be in ascending order")
        self.domain, self.link_config, self.acquisition_config = domain, link, acquisition
        self.stages, self.channels = stages, channels
        self.capture_clock = CaptureClock(
            domain, edge=link.capture_edge, phase_ticks=link.dco_phase_ps
        )
        self.link = StreamLink(domain, link, channels)
        self.acquisition = StreamAcquisition(
            domain, acquisition, channels, capture_clock=self.capture_clock
        )
        self.transport = TransportSimulation(stages)
        self.pending: dict[int, CapturedPacket] = {}
        self.next_packet_id = 0
        self.watermark: int | None = None
        self.closed = False
        self._result = DigitalBatch(stats=[StageStats(stage.name) for stage in stages])

    @property
    def pending_packet_count(self) -> int:
        return len(self.pending)

    def session_header(self, session_id: int) -> SessionHeader:
        """Describe decoded source words and the explicit capture-grid window policy."""
        if self.link.identity is not None and session_id != self.link.identity[2]:
            raise ValueError("raw session must match the source sample identity")
        source = ConvertedSamples(
            np.empty((0, len(self.channels)), np.uint16),
            self.channels,
            0,
            0,
            self.domain,
            self.link_config.bits,
            session_id=session_id,
        )
        header = session_header(source, self.acquisition_config, session_id)
        header.windows.update(
            selection_basis="capture_clock",
            capture_clock=asdict(self.capture_clock),
            link_model=asdict(self.link_config),
            packetization="contiguous source epoch segments within each capture window",
        )
        return header

    def schedule_syncs(self, ticks: list[int]) -> None:
        if self.closed:
            raise ValueError("digital input has finished")
        self.acquisition.schedule_syncs(ticks)

    def _packets(self, window: CapturedWindow) -> list[CapturedPacket]:
        groups: list[list[ConvertedSamples]] = []
        for segment in window.segments:
            previous = groups[-1][-1] if groups else None
            if (
                previous is not None
                and previous.epoch == segment.epoch
                and previous.first_index + len(previous) == segment.first_index
            ):
                groups[-1].append(segment)
            else:
                groups.append([segment])
        packets = []
        for index, group in enumerate(groups):
            first = group[0]
            source = ConvertedSamples(
                np.concatenate([part.codes for part in group]),
                self.channels,
                first.first_index,
                first.epoch,
                self.domain,
                first.bits,
                first.run_id,
                first.branch_id,
                first.session_id,
                {key: np.concatenate([part.meta[key] for part in group]) for key in first.meta},
            )
            assert window.completion_tick is not None
            item = Item(
                self.next_packet_id,
                source.codes.nbytes + 64,
                source.epoch,
                self.domain.id,
                source.first_index,
                len(source),
                window.completion_tick,
                window.line_number,
            )
            flags = (FLAG_LINE_START if index == 0 else 0) | (
                FLAG_LINE_END if index == len(groups) - 1 else 0
            )
            packets.append(
                CapturedPacket(
                    item,
                    source,
                    flags,
                    {
                        "sync_tick": str(window.sync_tick),
                        "capture_start_index": str(window.capture_start_index),
                        "window_length": window.window_length,
                        "missing_count": window.missing_count,
                        "termination_reason": window.termination_reason,
                        "clock_basis": window.clock_basis,
                    },
                )
            )
            self.next_packet_id += 1
        return packets

    def _take_transport(self) -> None:
        batch = self.transport.drain()
        for item, tick in batch.delivered:
            self._result.delivered.append(DeliveredPacket(self.pending.pop(item.item_id), tick))
        for loss in batch.losses:
            self.pending.pop(loss.item_id)
            self._result.losses.append(loss)
        for target, source in zip(self._result.stats, batch.stats, strict=True):
            target.occupancy.extend(source.occupancy)
            target.peak_bytes, target.max_wait_ps = source.peak_bytes, source.max_wait_ps
            target.overflow_count = source.overflow_count

    def _collect(self, tick: int | None, final: bool = False) -> None:
        for output in self.link.drain():
            self.acquisition.push(receive(output, self.link_config))
        if tick is not None:
            self.acquisition.advance(tick)
        if final:
            self.acquisition.finish()
        batch = self.acquisition.drain()
        self._result.ignored_syncs.extend(batch.ignored_syncs)
        self._result.skipped_lines.extend(batch.skipped_lines)
        packets = [packet for window in batch.windows for packet in self._packets(window)]
        self.transport.schedule([packet.item for packet in packets])
        self.pending.update({packet.item.item_id: packet for packet in packets})
        self.transport.advance(tick)
        self._take_transport()

    def push(self, source: ConvertedSamples) -> None:
        if self.closed:
            raise ValueError("digital input has finished")
        self.link.push(source)
        if not len(source):
            return
        tick = source.sample_time(len(source) - 1)
        self.link.advance(tick)
        self._collect(tick)
        self.watermark = tick

    def advance_to(self, tick: int) -> None:
        """Close an inclusive physical tick without inventing another source row."""
        if (
            self.closed
            or type(tick) is not int
            or not -(2**63) <= tick < 2**63
            or self.watermark is not None
            and tick < self.watermark
        ):
            raise ValueError("digital time must be an open, monotonic signed integer ps")
        self.link.advance(tick)
        self._collect(tick)
        self.watermark = tick

    def finish(self) -> None:
        if self.closed:
            return
        self.link.finish()
        self.link.advance()
        # EOF is ordered after the previous closed producer instant; no additional ADC row is made.
        candidates = [
            value
            for value in (
                self.link.watermark,
                self.watermark + 1 if self.watermark is not None else None,
            )
            if value is not None
        ]
        tick = max(candidates) if candidates else None
        self._collect(tick, final=True)
        self.transport.advance()
        self._take_transport()
        self.watermark = self.transport.watermark if self.transport.watermark is not None else tick
        self.closed = True

    def drain(self) -> DigitalBatch:
        result = self._result
        self._result = DigitalBatch(stats=[StageStats(stage.name) for stage in self.stages])
        return result

    def _configuration(self) -> dict:
        return {
            "domain": asdict(self.domain),
            "link": asdict(self.link_config),
            "acquisition": asdict(self.acquisition_config),
            "channels": self.channels,
            "stages": [asdict(stage) for stage in self.stages],
        }

    @staticmethod
    def _hash(value: dict) -> str:
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, allow_nan=False).encode()
        ).hexdigest()

    def checkpoint(self) -> bytes:
        payload = {
            "format": self.FORMAT,
            "config_hash": self._hash(self._configuration()),
            "state": {
                "link": json.loads(self.link.checkpoint()),
                "acquisition": json.loads(self.acquisition.checkpoint()),
                "transport": json.loads(self.transport.checkpoint()),
                "next_packet_id": str(self.next_packet_id),
                "watermark": str(self.watermark) if self.watermark is not None else None,
                "closed": self.closed,
                "pending_packets": {
                    str(key): packet.to_dict() for key, packet in self.pending.items()
                },
                "delivered": [
                    {
                        "packet": delivered.packet.to_dict(),
                        "delivery_tick": str(delivered.delivery_tick),
                    }
                    for delivered in self._result.delivered
                ],
                "losses": [asdict(loss) for loss in self._result.losses],
                "ignored_syncs": [str(tick) for tick in self._result.ignored_syncs],
                "skipped_lines": [
                    [str(line), reason] for line, reason in self._result.skipped_lines
                ],
                "stats": [asdict(stat) for stat in self._result.stats],
            },
        }
        payload["checksum"] = self._hash(payload)
        return json.dumps(payload, sort_keys=True, allow_nan=False).encode()

    @classmethod
    def restore(
        cls,
        domain: ClockDomain,
        link: LinkConfig,
        acquisition: AcquisitionConfig,
        stages: list[StageSpec],
        channels: tuple[int, ...],
        blob: bytes,
    ) -> DigitalPipeline:
        payload = json.loads(blob)
        if not isinstance(payload, dict) or payload.get("format") != cls.FORMAT:
            raise ValueError("digital checkpoint version is unsupported")
        engine = cls(domain, link, acquisition, stages, channels)
        if payload.get("config_hash") != cls._hash(engine._configuration()):
            raise ValueError("digital checkpoint configuration differs")
        if payload.pop("checksum", None) != cls._hash(payload):
            raise ValueError("digital checkpoint checksum is invalid")
        state = payload["state"]
        engine.link = StreamLink.restore(domain, link, channels, json.dumps(state["link"]).encode())
        engine.acquisition = StreamAcquisition.restore(
            domain,
            acquisition,
            channels,
            json.dumps(state["acquisition"]).encode(),
            capture_clock=engine.capture_clock,
        )
        engine.transport = TransportSimulation.restore(
            stages, json.dumps(state["transport"]).encode()
        )
        engine.next_packet_id, engine.closed = int(state["next_packet_id"]), state["closed"]
        engine.watermark = int(state["watermark"]) if state["watermark"] is not None else None
        engine.pending = {
            int(key): CapturedPacket.from_dict(packet, domain, channels)
            for key, packet in state["pending_packets"].items()
        }
        engine._result = DigitalBatch(
            [
                DeliveredPacket(
                    CapturedPacket.from_dict(row["packet"], domain, channels),
                    int(row["delivery_tick"]),
                )
                for row in state["delivered"]
            ],
            [TransportLoss(**row) for row in state["losses"]],
            [int(tick) for tick in state["ignored_syncs"]],
            [(int(line), reason) for line, reason in state["skipped_lines"]],
            [
                StageStats(**{**row, "occupancy": [tuple(pair) for pair in row["occupancy"]]})
                for row in state["stats"]
            ],
        )
        if set(engine.pending) != {item.item_id for item in engine.transport.result().in_flight}:
            raise ValueError("digital checkpoint queued payloads do not match in-flight metadata")
        return engine
