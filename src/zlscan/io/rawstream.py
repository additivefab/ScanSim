"""ZLRAW 0.1: the raw ADC stream container (docs/dev/03_contracts.md section 5; DRAFT).

The format is a draft until it is frozen together with the FPGA side; the version stays 0.x.

    file    = PRELUDE "ZLRAW0.1"  |  u32 json_len  |  session header (UTF-8 JSON)  |  block*
    block   = 64-byte header (little endian, CRC-protected)  |  payload
    payload = sample_count rows of channels*bytes_per_sample bytes (channel-interleaved)

The reader's rule is that nothing is stitched silently. Damage either becomes an explicit
``RawError`` or a ``Loss`` with the exact range of source samples it covers, and a damaged
block's data is never delivered. Header-level damage is fatal because a corrupt header cannot be
trusted to say where the next block starts.
"""

from __future__ import annotations

import os
import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict

PRELUDE = b"ZLRAW0.1"
BLOCK_MAGIC = b"ZLR1"
BLOCK_VERSION = 1
BLOCK_HEADER_SIZE = 64
_HEAD = struct.Struct("<4sHHQIIHHHHQIIIII")  # the first 60 bytes
assert _HEAD.size == 60

FLAG_LINE_START = 0x01
FLAG_LINE_END = 0x02
FLAG_FRAME_START = 0x04
FLAG_FRAME_END = 0x08
FLAG_OVERFLOW_AFTER = 0x10
FLAG_EPOCH_START = 0x20


class SessionHeader(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1"]
    session_id: int
    created_at: str
    hardware_hash: str | None
    firmware_hash: str | None
    wiring_rev: str | None
    register_dump_hash: str | None
    calibration_id: str | None
    channel_roles: dict[str, str | None]
    clock_domains: list[dict[str, Any]]
    coding: Literal["offset_binary", "twos_complement"]
    bits_per_sample: int
    bytes_per_sample: Literal[2]
    value_domain: Literal["raw_code"] = "raw_code"
    windows: dict[str, Any]
    origin: Literal["SIMULATED", "MEASURED"]


@dataclass
class RawError:
    code: str
    message: str
    offset: int


@dataclass
class Loss:
    layer: str
    epoch: int
    clock_domain: int
    first_sample_index: int
    last_sample_index: int
    reason: str
    certainty: str  # "exact" | "estimated"


@dataclass
class Segment:
    """A run of consecutive source samples that were delivered intact."""

    epoch: int
    clock_domain: int
    channels: list[int]
    first_sample_index: int
    samples: np.ndarray  # shape (rows, len(channels)), little-endian uint16


@dataclass
class BlockInfo:
    """One block that was delivered intact (header fields the reader verified)."""

    seq: int
    epoch: int
    clock_domain: int
    channels: list[int]
    first_sample_index: int
    sample_count: int
    flags: int
    line_number: int
    payload_offset: int | None = None
    payload_crc32: int | None = None


@dataclass
class ReadResult:
    session: SessionHeader | None = None
    blocks: list[BlockInfo] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    losses: list[Loss] = field(default_factory=list)
    errors: list[RawError] = field(default_factory=list)
    fatal: bool = False


def _channels(mask: int) -> list[int]:
    return [i for i in range(16) if mask >> i & 1]


class RawWriter:
    def __init__(self, path: Path, session: SessionHeader) -> None:
        self.session = session
        self._seq = 0
        self._file = open(path, "wb")  # noqa: SIM115 - closed in __exit__
        meta = session.model_dump_json().encode("utf-8")
        self._file.write(PRELUDE + struct.pack("<I", len(meta)) + meta)

    def __enter__(self) -> RawWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self._file.close()

    def write_block(
        self,
        *,
        epoch: int,
        channel_mask: int,
        clock_domain: int,
        first_sample_index: int,
        samples: np.ndarray,
        flags: int = 0,
        line_number: int = 0,
    ) -> None:
        samples = np.asarray(samples)
        if samples.dtype not in (np.dtype("<u2"), np.dtype("uint16")):
            raise ValueError("samples must be uint16")
        channels = _channels(channel_mask)
        if samples.ndim != 2 or samples.shape[1] != len(channels):
            raise ValueError(f"channel count of samples disagrees with the mask {channel_mask:#b}")
        if samples.size and int(samples.max()) >= 2**self.session.bits_per_sample:
            raise ValueError(
                f"a sample does not fit the declared {self.session.bits_per_sample} bits"
            )
        payload = np.ascontiguousarray(samples, dtype="<u2").tobytes()
        head = _HEAD.pack(
            BLOCK_MAGIC, BLOCK_VERSION, BLOCK_HEADER_SIZE, self.session.session_id, epoch,
            self._seq, channel_mask, clock_domain, flags, 0, first_sample_index,
            samples.shape[0], len(payload), line_number, zlib.crc32(payload), 0,
        )  # fmt: skip
        self._file.write(head + struct.pack("<I", zlib.crc32(head)) + payload)
        self._seq += 1


def read_all(path: Path) -> ReadResult:
    """Read the capture and retain contiguous sample segments for existing callers."""
    with Path(path).open("rb") as stream:
        return _read(stream, None)


def scan_blocks(
    path: Path, consume: Callable[[SessionHeader, BlockInfo, np.ndarray], None]
) -> ReadResult:
    """Validate a capture and deliver each intact block without retaining payloads."""
    with Path(path).open("rb") as stream:
        return _read(stream, consume)


def _read(
    stream: BinaryIO, consume: Callable[[SessionHeader, BlockInfo, np.ndarray], None] | None
) -> ReadResult:
    size = os.fstat(stream.fileno()).st_size
    result = ReadResult()

    def fail(code: str, message: str, offset: int, fatal: bool = False) -> None:
        result.errors.append(RawError(code, message, offset))
        result.fatal = result.fatal or fatal

    if stream.read(len(PRELUDE)) != PRELUDE:
        fail("bad_file_magic", "not a ZLRAW 0.1 file", 0, fatal=True)
        return result
    pos = len(PRELUDE)
    length = stream.read(4)
    if len(length) != 4:
        fail("truncated", "file ends inside the session header length", pos, fatal=True)
        return result
    (json_len,) = struct.unpack("<I", length)
    pos += 4
    if json_len > size - pos:
        fail("bad_session_header", "file ends inside the session header", pos, fatal=True)
        return result
    try:
        result.session = SessionHeader.model_validate_json(stream.read(json_len))
    except ValueError as exc:
        fail("bad_session_header", str(exc).splitlines()[0], pos, fatal=True)
        return result
    pos += json_len
    session = result.session

    prev_seq: int | None = None
    prev_epoch: int | None = None
    next_index: dict[tuple[int, int], int] = {}
    open_segment: dict[tuple[int, int], Segment] = {}
    bps = session.bytes_per_sample

    while pos < size:
        raw = stream.read(BLOCK_HEADER_SIZE)
        if len(raw) < BLOCK_HEADER_SIZE:
            fail("truncated", "file ends inside a block header", pos)
            break
        if raw[:4] != BLOCK_MAGIC:
            fail("bad_block_magic", "block magic is wrong", pos, fatal=True)
            break
        if struct.unpack_from("<I", raw, 60)[0] != zlib.crc32(raw[:60]):
            fail("header_crc", "block header CRC mismatch", pos, fatal=True)
            break
        (magic, version, header_len, session_id, epoch, seq, mask, domain, flags, _r1, first,
         count, payload_bytes, line_number, payload_crc, _r2) = _HEAD.unpack_from(raw)  # fmt: skip
        if version != BLOCK_VERSION:
            fail("unknown_version", f"block format version {version} is not supported", pos, True)
            break
        if header_len != BLOCK_HEADER_SIZE:
            fail("bad_header_len", f"header length {header_len} is not 64", pos, fatal=True)
            break
        channels = _channels(mask)
        if payload_bytes != count * len(channels) * bps:
            fail("payload_length_mismatch", "payload length disagrees with the header", pos, True)
            break
        if payload_bytes > size - pos - BLOCK_HEADER_SIZE:
            fail("truncated", "file ends inside a block payload", pos)
            break
        body = stream.read(payload_bytes)
        if len(body) != payload_bytes:
            fail("truncated", "file ends inside a block payload", pos)
            break
        block_at = pos
        pos += BLOCK_HEADER_SIZE + payload_bytes
        key = (epoch, domain)
        last = first + count - 1

        if session_id != session.session_id:
            fail("session_mismatch", f"block of session {session_id} in session "
                 f"{session.session_id}", block_at)  # fmt: skip
            continue
        if prev_seq is not None and seq <= prev_seq:
            fail("sequence_regress", f"block sequence {seq} after {prev_seq}", block_at)
            continue
        if prev_seq is not None and seq > prev_seq + 1:
            fail("block_sequence_gap", f"block sequence jumps from {prev_seq} to {seq}", block_at)
        if prev_epoch is not None and epoch != prev_epoch and not flags & FLAG_EPOCH_START:
            fail(
                "epoch_changed_without_flag",
                f"epoch {prev_epoch} -> {epoch} without a flag",
                block_at,
            )
        expected = next_index.get(key)
        if expected is not None and first < expected:
            fail(
                "sample_index_regress", f"samples restart at {first}, expected {expected}", block_at
            )
            continue
        line_structured = bool(flags & FLAG_LINE_START)
        if expected is not None and first > expected:
            # Between line windows a sample gap is by design. Then a loss is only inferred from a
            # jump in the block sequence, and its range is an estimate (it may include samples
            # that were never meant to be kept).
            lost_blocks = prev_seq is not None and seq > prev_seq + 1
            if not line_structured or lost_blocks:
                result.losses.append(
                    Loss("capture", epoch, domain, expected, first - 1, "missing_blocks",
                         "estimated" if line_structured else "exact")
                )  # fmt: skip
            open_segment.pop(key, None)
        prev_seq, prev_epoch, next_index[key] = seq, epoch, last + 1

        if zlib.crc32(body) != payload_crc:
            fail("payload_crc", "block payload CRC mismatch; the block is not used", block_at)
            result.losses.append(
                Loss("capture", epoch, domain, first, last, "payload_crc", "exact")
            )
            open_segment.pop(key, None)
            continue
        rows = np.frombuffer(body, dtype="<u2").reshape(count, len(channels))
        block = BlockInfo(
            seq,
            epoch,
            domain,
            channels,
            first,
            count,
            flags,
            line_number,
            block_at + BLOCK_HEADER_SIZE,
            payload_crc,
        )
        result.blocks.append(block)
        if consume is not None:
            consume(session, block, rows)
            continue
        segment = open_segment.get(key)
        if segment is None or segment.channels != channels:
            segment = Segment(epoch, domain, channels, first, rows.copy())
            result.segments.append(segment)
            open_segment[key] = segment
        else:
            segment.samples = np.concatenate([segment.samples, rows])
    return result


def read_headerless_u16(
    path: Path, *, nx: int, ny: int, samples_per_pixel: int, coding: str
) -> np.ndarray:
    """Read package B's headerless region-of-interest file (little-endian uint16, row-major).

    The shape and the coding must be stated by the caller; a file of the wrong size is an error,
    never padded or cropped. Twos-complement words are converted to offset binary.
    """
    if coding not in ("offset_binary", "twos_complement"):
        raise ValueError("coding must be 'offset_binary' or 'twos_complement'")
    raw = np.fromfile(path, dtype="<u2")
    expected = ny * nx * samples_per_pixel
    if raw.size != expected:
        raise ValueError(
            f"expected {expected} samples ({expected * 2} bytes) but the file holds {raw.size}"
        )
    if coding == "twos_complement":
        raw = np.bitwise_xor(raw, np.uint16(0x8000))
    return raw.reshape(ny, nx * samples_per_pixel)
