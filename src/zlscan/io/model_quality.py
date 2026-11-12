"""Committed model-quality bit vectors, indexed on disk and isolated from observations."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import tempfile
import zlib
from dataclasses import asdict
from pathlib import Path

import numpy as np

from zlscan.contracts.time import ClockDomain
from zlscan.io.committed import CommittedFile, verify_committed_prefix
from zlscan.trace.window import validate_window

GRIDS = {"adc_saturated": "adc", "front_end_clipped": "fine", "pmt_over_limit": "fine"}
MAX_STEPS = 1_000_000
MAX_ROW = 8 << 20


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def quality_header(channels, os, adc_clock, fine_clock, identity, base_hashes, model_version):
    if (
        not channels
        or len(channels) > 16
        or len(set(channels)) != len(channels)
        or any(type(c) is not int or not 0 <= c < 16 for c in channels)
        or type(os) is not int
        or os < 2
        or fine_clock.freq_num * adc_clock.freq_den != adc_clock.freq_num * os * fine_clock.freq_den
        or fine_clock.phase_ticks != adc_clock.phase_ticks
    ):
        raise ValueError("model quality channel or native clock basis differs")
    if (
        set(identity) != {"run_id", "branch_id", "session_id"}
        or not all(isinstance(identity[k], str) for k in ["run_id", "branch_id"])
        or type(identity["session_id"]) is not int
        or not 0 <= identity["session_id"] < 2**64
        or set(base_hashes) != {str(c) for c in channels}
        or any(
            not isinstance(h, str) or len(h) != 64 or any(c not in "0123456789abcdef" for c in h)
            for h in base_hashes.values()
        )
        or not isinstance(model_version, str)
        or not model_version
    ):
        raise ValueError("model quality source identity differs")
    return dict(
        format="zlscan-model-quality/1",
        origin="SIMULATED",
        visibility="ORACLE_ONLY",
        channels=list(channels),
        os=os,
        adc_clock=asdict(adc_clock),
        fine_clock=asdict(fine_clock),
        identity=dict(identity),
        base_hashes=dict(base_hashes),
        model_version=model_version,
        grids=GRIDS,
    )


def _validate_header(header):
    expected = quality_header(
        tuple(header["channels"]),
        header["os"],
        ClockDomain(**header["adc_clock"]),
        ClockDomain(**header["fine_clock"]),
        header["identity"],
        header["base_hashes"],
        header["model_version"],
    )
    if header != expected:
        raise ValueError("model quality header differs")
    return expected


def validate_checkpoint(state):
    value = json.loads(json.dumps(state, allow_nan=False))
    if (
        value.pop("checksum", None) != digest(value)
        or value.get("format") != "zlscan-model-quality-checkpoint/1"
    ):
        raise ValueError("model quality checkpoint checksum/version differs")
    _validate_header(value["header"])
    if not 0 <= int(value["records"]) <= int(value["step"]) < 2**64:
        raise ValueError("model quality checkpoint header or counters differ")
    return value


def _count(header, start, end, kind):
    return (
        end - start
        if GRIDS[kind] == "fine"
        else -(-end // header["os"]) - -(-start // header["os"])
    )


def _row(header, row, start, max_end):
    end = int(row["end"])
    if (
        set(row) != {"start", "end", "epoch", "flags"}
        or int(row["start"]) != start
        or not start < end <= max_end
        or end - start > MAX_STEPS
        or type(row["epoch"]) is not int
        or not 0 <= row["epoch"] < 2**32
        or set(row["flags"]) != {str(c) for c in header["channels"]}
    ):
        raise ValueError("model quality row boundary or identity differs")
    for arrays in row["flags"].values():
        if set(arrays) != set(GRIDS) or any(not isinstance(v, str) for v in arrays.values()):
            raise ValueError("model quality vectors differ")
    return end


def _decode(encoded, count):
    if not 0 <= count <= MAX_STEPS:
        raise ValueError("model quality decode exceeds software budget")
    size = -(-count // 8)
    inflater = zlib.decompressobj()
    try:
        raw = inflater.decompress(base64.b64decode(encoded, validate=True), size + 1)
    except (ValueError, zlib.error) as exc:
        raise ValueError("invalid compressed model quality vector") from exc
    if (
        len(raw) != size
        or not inflater.eof
        or inflater.unused_data
        or inflater.unconsumed_tail
        or count % 8
        and raw[-1] >> (count % 8)
    ):
        raise ValueError("model quality compressed layout differs")
    return np.unpackbits(np.frombuffer(raw, dtype=np.uint8), bitorder="little")[:count]


class QualityWriter:
    def __init__(self, path: Path, header: dict):
        self.path, self.header = Path(path), _validate_header(json.loads(json.dumps(header)))
        self.file = CommittedFile(self.path)
        try:
            self.file.append((json.dumps(self.header, sort_keys=True) + "\n").encode())
        except BaseException:
            self.file.close()
            raise
        self.step = self.records = 0

    def append(self, start: int, end: int, epoch: int, flags: dict) -> None:
        if start != self.step or not start < end < 2**64 or end - start > MAX_STEPS:
            raise ValueError("model quality append boundary exceeds grid budget")
        if set(flags) != set(self.header["channels"]):
            raise ValueError("model quality channels differ")
        row = dict(start=str(start), end=str(end), epoch=epoch, flags={})
        for channel, arrays in flags.items():
            if set(arrays) != set(GRIDS):
                raise ValueError("model quality kinds differ")
            encoded = {}
            for kind, array in arrays.items():
                n = _count(self.header, start, end, kind)
                if array.shape != (n,) or array.dtype != np.dtype(bool):
                    raise ValueError("model quality vector shape or native grid differs")
                packed = np.packbits(array, bitorder="little").tobytes()
                encoded[kind] = base64.b64encode(zlib.compress(packed, level=1)).decode("ascii")
            row["flags"][str(channel)] = encoded
        _row(self.header, row, start, end)
        data = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
        if len(data) > MAX_ROW:
            raise ValueError("model quality encoded row exceeds software budget")
        self.file.append(data)
        self.step, self.records = end, self.records + 1

    def checkpoint(self) -> dict:
        value = dict(
            format="zlscan-model-quality-checkpoint/1",
            header=self.header,
            step=str(self.step),
            records=str(self.records),
            file=self.file.checkpoint(),
        )
        value["checksum"] = digest(value)
        return value

    @classmethod
    def restore(
        cls, path: Path, state: dict, *, branch_path: Path | None = None, expected_epoch=None
    ):
        with QualityReader(path, state) as reader:
            if expected_epoch is not None:
                reader.check_epochs(expected_epoch)
        value = validate_checkpoint(state)
        result = cls.__new__(cls)
        result.path, result.header = Path(branch_path or path), value["header"]
        result.file = CommittedFile.resume(path, value["file"], branch_path=branch_path)
        result.step, result.records = int(value["step"]), int(value["records"])
        return result

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.file.close()


class QualityReader:
    def __init__(self, path: Path, state: dict):
        self.path, self.state = Path(path), validate_checkpoint(state)
        self.header = self.state["header"]
        self.step = int(self.state["step"])
        self.clock = ClockDomain(**self.header["fine_clock"])
        self.temporary = tempfile.TemporaryDirectory(prefix="zlscan-model-quality-")
        self.database = Path(self.temporary.name) / "index.sqlite"
        connection = None
        try:
            self.unchanged()
            connection = sqlite3.connect(self.database)
            connection.execute("PRAGMA cache_size=-2048")
            connection.execute(
                "CREATE TABLE blocks(lo TEXT,hi TEXT,position INTEGER,length INTEGER,epoch INTEGER)"
            )
            with self.path.open("rb") as stream:
                limit = int(self.state["file"]["offset"])
                first = stream.readline(min(limit, 65536))
                if not first.endswith(b"\n") or json.loads(first) != self.header:
                    raise ValueError("model quality header differs")
                step = records = 0
                while stream.tell() < limit:
                    position = stream.tell()
                    line = stream.readline(min(limit - position, MAX_ROW + 1))
                    if len(line) > MAX_ROW or not line.endswith(b"\n"):
                        raise ValueError("model quality row exceeds budget or is incomplete")
                    row = json.loads(line)
                    end = _row(self.header, row, step, self.step)
                    for arrays in row["flags"].values():
                        for kind, encoded in arrays.items():
                            _decode(encoded, _count(self.header, step, end, kind))
                    connection.execute(
                        "INSERT INTO blocks VALUES(?,?,?,?,?)",
                        (f"{step:020d}", f"{end:020d}", position, len(line), row["epoch"]),
                    )
                    step, records = end, records + 1
                if step != self.step or records != int(self.state["records"]):
                    raise ValueError("model quality index and checkpoint differ")
            connection.execute("CREATE INDEX query_blocks ON blocks(hi)")
            connection.commit()
            self.unchanged()
        except BaseException:
            if connection is not None:
                connection.close()
                connection = None
            self.temporary.cleanup()
            raise
        finally:
            if connection is not None:
                connection.close()

    def unchanged(self):
        return verify_committed_prefix(self.path, self.state["file"])

    def check_epochs(self, expected):
        connection = sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)
        try:
            for start, end, epoch in connection.execute("SELECT lo,hi,epoch FROM blocks"):
                if expected(int(start)) != epoch or expected(int(end) - 1) != epoch:
                    raise ValueError("model quality epoch differs from physical control history")
        finally:
            connection.close()

    def query(
        self, channel: int, start: int, end: int, kinds: set | None, limit: int = 4096
    ) -> dict:
        validate_window(start, end, 1)
        if (
            channel not in self.header["channels"]
            or type(limit) is not int
            or not 1 <= limit <= 4097
            or kinds is not None
            and set(kinds) - set(GRIDS)
        ):
            raise ValueError("unknown quality kind/channel or event limit")
        self.unchanged()
        requested = [str(start), str(end)]
        start, end = max(0, start), min(self.step, end)
        if end <= start:
            return dict(status="not_recorded", events=[], reason="outside committed quality span")
        selected = set(GRIDS) if kinds is None else set(kinds)
        output = []
        connection = sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)
        connection.execute("PRAGMA cache_size=-2048")
        try:
            cursor = connection.execute(
                "SELECT * FROM blocks WHERE hi>? ORDER BY hi", (f"{start:020d}",)
            )
            with self.path.open("rb") as stream:
                for lo, hi, position, length, _epoch in cursor:
                    lo, hi = int(lo), int(hi)
                    if lo >= end:
                        break
                    stream.seek(position)
                    row = json.loads(stream.read(length))
                    candidates = []
                    needed = limit + 1 - len(output)
                    for kind in sorted(selected):
                        mask = _decode(
                            row["flags"][str(channel)][kind], _count(self.header, lo, hi, kind)
                        )
                        stride = self.header["os"] if GRIDS[kind] == "adc" else 1
                        origin = -(-lo // stride) * stride
                        left = max(0, -(-(start - origin) // stride))
                        right = min(len(mask), -(-(end - origin) // stride))
                        found = 0
                        for first in range(left, right, 4096):
                            for offset in np.flatnonzero(mask[first : min(right, first + 4096)]):
                                step = origin + (first + int(offset)) * stride
                                candidates.append(
                                    dict(
                                        kind=kind,
                                        step=step,
                                        step_exact=str(step),
                                        source_sample_index=str(step // stride)
                                        if stride != 1
                                        else None,
                                        sample_grid=GRIDS[kind],
                                        epoch=row["epoch"],
                                        channel=channel,
                                        time_ticks=str(self.clock.tick_of(step)),
                                        origin="SIMULATED",
                                        visibility="ORACLE_ONLY",
                                        derivation="recorded",
                                        source="original native-grid model flag",
                                        session_id=str(self.header["identity"]["session_id"]),
                                    )
                                )
                                found += 1
                                if found >= needed:
                                    break
                            if found >= needed:
                                break
                    candidates.sort(key=lambda r: (r["step"], r["kind"]))
                    output.extend(candidates[:needed])
                    if len(output) > limit:
                        break
        finally:
            connection.close()
        self.unchanged()
        return dict(
            status="exact_record",
            events=output[:limit],
            truncated=len(output) > limit,
            origin="SIMULATED",
            visibility="ORACLE_ONLY",
            grids={k: GRIDS[k] for k in selected},
            support_start=str(start),
            support_end=str(end),
            requested_window=requested,
            clipped_window=requested != [str(start), str(end)],
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.temporary.cleanup()
