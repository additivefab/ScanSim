"""Immutable HDF5 probe shards paired with one committed append-index cursor.

Only display summaries and explicitly requested research raw samples live here. The
producer's atomic root owns this component's checkpoint; it is never a second root.
"""

from __future__ import annotations

import json
import math
import os
import shutil
from copy import deepcopy
from pathlib import Path
from typing import Literal

import h5py
import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from zlscan.io.committed import CommittedFile, verify_committed_prefix
from zlscan.io.manifest import sha256_file
from zlscan.trace.window import validate_window

LEVELS = 64
MAX_CHUNK = 1_000_000
Probe = Literal["x_m", "t_eff", "power_w", "current_a", "v_adc"]


class TracePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    probes: tuple[Probe, ...] = Field(min_length=1, max_length=5)
    base: int = Field(default=64, ge=2, le=4096)
    factor: int = Field(default=8, ge=2, le=16)
    keep_raw: bool = False

    @model_validator(mode="after")
    def distinct(self):
        if len(set(self.probes)) != len(self.probes):
            raise ValueError("probe names must be unique")
        # All representable fine-step spans must have a top summary bucket.
        if self.base * self.factor ** (LEVELS - 1) < 2**64:
            raise ValueError("summary factor too small for the fixed uint64 tier budget")
        return self


def _merge(a, b):
    if a is None:
        return list(b)
    if a[0] + a[1] != b[0]:
        raise ValueError("summary inputs are not adjacent")
    return [a[0], a[1] + b[1], min(a[2], b[2]), max(a[3], b[3]), a[4], b[5]]


def _digest(value):
    import hashlib

    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _header(policy, channels):
    return dict(
        format="zlscan-probe-shards/1",
        origin="SIMULATED",
        visibility="ORACLE_ONLY",
        policy=policy.model_dump(mode="json"),
        channels=list(channels),
    )


def validate_checkpoint(value):
    value = deepcopy(value)
    if value.pop("checksum", None) != _digest(value):
        raise ValueError("probe checkpoint checksum differs")
    if value.get("format") != "zlscan-probe-checkpoint/1":
        raise ValueError("probe checkpoint version differs")
    policy = TracePolicy.model_validate_json(json.dumps(value["header"]["policy"]))
    channels = tuple(value["header"]["channels"])
    if (
        not channels
        or len(channels) > 16
        or len(set(channels)) != len(channels)
        or any(type(c) is not int or not 0 <= c < 16 for c in channels)
        or value["header"] != _header(policy, channels)
    ):
        raise ValueError("probe checkpoint channels or provenance differ")
    step, sequence = int(value["step"]), int(value["sequence"])
    if not 0 <= sequence <= step < 2**64:
        raise ValueError("probe checkpoint span differs")
    keys = {f"{c}/{p}" for c in channels for p in policy.probes}
    if set(value["pending"]) != keys:
        raise ValueError("probe pending identities differ")
    for rows in value["pending"].values():
        if len(rows) != LEVELS:
            raise ValueError("probe pending tiers differ")
        child = 1
        for level, row in enumerate(rows):
            size = policy.base if level == 0 else policy.factor
            count = (step // child % size) * child
            if count == 0:
                if row is not None:
                    raise ValueError("unexpected pending summary")
            elif (
                not isinstance(row, list)
                or len(row) != 6
                or type(row[0]) is not int
                or type(row[1]) is not int
                or row[:2] != [step // child * child - count, count]
                or any(not isinstance(v, (float, int)) or not math.isfinite(v) for v in row[2:])
                or not row[2] <= row[4] <= row[3]
                or not row[2] <= row[5] <= row[3]
            ):
                raise ValueError("pending summary boundary or values differ")
            child *= size
    return value, policy, channels


def _entries(directory, state):
    path = directory / "index.jsonl"
    verify_committed_prefix(path, state["index"])
    with path.open("rb") as stream:
        end = int(state["index"]["offset"])
        header = json.loads(stream.readline(min(end, 65536)))
        if header != state["header"]:
            raise ValueError("probe index header differs")
        first = sequence = 0
        while stream.tell() < end:
            line = stream.readline(min(end - stream.tell(), 65536))
            if not line.endswith(b"\n"):
                raise ValueError("probe index row exceeds budget or is incomplete")
            row = json.loads(line)
            if (
                row["sequence"] != sequence
                or row["start"] != first
                or not first < row["end"] <= int(state["step"])
                or row["end"] - first > MAX_CHUNK
            ):
                raise ValueError("probe index boundaries differ")
            sequence += 1
            first = row["end"]
            yield row
        if first != int(state["step"]) or sequence != int(state["sequence"]):
            raise ValueError("probe index and checkpoint differ")


def _shard(directory, row):
    path = directory / f"{row['sequence']:020d}.h5"
    if sha256_file(path) != row["sha256"]:
        raise ValueError("probe shard SHA mismatch")
    return path


class TraceWriter:
    def __init__(self, directory: Path, policy: TracePolicy, channels: tuple[int, ...]):
        policy = TracePolicy.model_validate_json(policy.model_dump_json())
        if (
            not channels
            or len(channels) > 16
            or len(set(channels)) != len(channels)
            or any(type(c) is not int or not 0 <= c < 16 for c in channels)
        ):
            raise ValueError("probe channels must be distinct explicit channel IDs")
        self.directory, self.policy, self.channels = Path(directory), policy, channels
        self.directory.mkdir(parents=True, exist_ok=False)
        self.index = CommittedFile(self.directory / "index.jsonl")
        self.header = _header(policy, channels)
        self.index.append((json.dumps(self.header, sort_keys=True) + "\n").encode())
        self.step = self.sequence = 0
        self.pending = {f"{c}/{p}": [None] * LEVELS for c in channels for p in policy.probes}
        self.failed = False

    def _push(self, rows, level, value, completed):
        pending = self.pending[rows]
        pending[level] = _merge(pending[level], value)
        size = self.policy.base * self.policy.factor**level
        if pending[level][1] == size:
            value, pending[level] = pending[level], None
            completed.setdefault(level, []).append(value)
            if level + 1 < LEVELS:
                self._push(rows, level + 1, value, completed)

    def append(self, start: int, arrays: dict[int, dict[str, np.ndarray]]) -> None:
        if self.failed:
            raise OSError("probe writer failed; restore a committed root")
        if start != self.step or set(arrays) != set(self.channels):
            raise ValueError("probe append channel or step differs")
        n = None
        for probes in arrays.values():
            if set(probes) != set(self.policy.probes):
                raise ValueError("probe append selection differs")
            for array in probes.values():
                if array.ndim != 1 or not np.isfinite(array).all():
                    raise ValueError("probe samples must be finite vectors")
                n = len(array) if n is None else n
                if len(array) != n:
                    raise ValueError("probe vectors have different lengths")
        if n is None or not 0 < n <= MAX_CHUNK or start + n >= 2**64:
            raise ValueError("probe append exceeds fine-step chunk budget")
        path = self.directory / f"{self.sequence:020d}.h5"
        try:
            # HDF5 metadata is never modified after this file is closed and indexed.
            with h5py.File(path, "x") as file:
                file.attrs["header"] = json.dumps(self.header, sort_keys=True)
                file.attrs["start"], file.attrs["end"] = str(start), str(start + n)
                for channel in self.channels:
                    for probe in self.policy.probes:
                        group = file.create_group(f"{channel}/{probe}")
                        data = arrays[channel][probe]
                        if self.policy.keep_raw:
                            group.create_dataset(
                                "raw", data=data, dtype="<f8", chunks=True, track_times=False
                            )
                        completed = {}
                        key = f"{channel}/{probe}"
                        position = 0
                        while position < n:
                            take = min(
                                n - position,
                                self.policy.base
                                - (self.pending[key][0][1] if self.pending[key][0] else 0),
                            )
                            part = data[position : position + take]
                            self._push(
                                key,
                                0,
                                [
                                    start + position,
                                    take,
                                    float(part.min()),
                                    float(part.max()),
                                    float(part[0]),
                                    float(part[-1]),
                                ],
                                completed,
                            )
                            position += take
                        for level, rows in completed.items():
                            target = group.create_group(str(level))
                            target.create_dataset(
                                "start", data=[r[0] for r in rows], dtype="<u8", track_times=False
                            )
                            target.create_dataset(
                                "count", data=[r[1] for r in rows], dtype="<u8", track_times=False
                            )
                            target.create_dataset(
                                "values",
                                data=[r[2:] for r in rows],
                                dtype="<f8",
                                chunks=True,
                                track_times=False,
                            )
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
            row = dict(sequence=self.sequence, start=start, end=start + n, sha256=sha256_file(path))
            self.index.append((json.dumps(row, sort_keys=True) + "\n").encode())
            self.sequence += 1
            self.step += n
        except BaseException:
            self.failed = True
            raise

    def checkpoint(self) -> dict:
        if self.failed:
            raise OSError("probe writer failed; restore a committed root")
        result = dict(
            format="zlscan-probe-checkpoint/1",
            header=self.header,
            step=str(self.step),
            sequence=str(self.sequence),
            pending=deepcopy(self.pending),
            index=self.index.checkpoint(),
        )
        # Persist directory entries before the parent publishes its root.
        if os.name != "nt":
            fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        result["checksum"] = _digest(result)
        return result

    @classmethod
    def restore(cls, directory: Path, state: dict, *, branch_directory: Path | None = None):
        value, policy, channels = validate_checkpoint(state)
        source = Path(directory)
        # Verify every referenced shard before opening an append handle.
        for row in _entries(source, value):
            _shard(source, row)
        if branch_directory is None:
            for path in source.iterdir():
                if path.name == "index.jsonl":
                    continue
                if (
                    not path.name.endswith(".h5")
                    or not path.stem.isdecimal()
                    or len(path.stem) != 20
                    or int(path.stem) >= int(value["sequence"])
                ):
                    raise ValueError("uncommitted orphan probe shard; choose a branch")
        target = Path(branch_directory) if branch_directory is not None else source
        if branch_directory is not None:
            target.mkdir(parents=True, exist_ok=False)
        result = cls.__new__(cls)
        result.directory, result.policy, result.channels = target, policy, channels
        result.index = CommittedFile.resume(
            source / "index.jsonl",
            value["index"],
            branch_path=target / "index.jsonl" if branch_directory is not None else None,
        )
        try:
            if branch_directory is not None:
                for row in _entries(source, value):
                    path = _shard(source, row)
                    with path.open("rb") as src, (target / path.name).open("xb") as dst:
                        shutil.copyfileobj(src, dst, 1 << 20)
                        dst.flush()
                        os.fsync(dst.fileno())
                    _shard(target, row)
            result.header, result.pending = value["header"], value["pending"]
            result.step, result.sequence = int(value["step"]), int(value["sequence"])
            result.failed = False
            return result
        except BaseException:
            result.index.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.index.close()


class TraceReader:
    def __init__(self, directory: Path, state: dict):
        self.directory = Path(directory)
        self.state, self.policy, self.channels = validate_checkpoint(state)
        self.step = int(self.state["step"])
        for row in _entries(self.directory, self.state):
            _shard(self.directory, row)
        self.unchanged()

    def unchanged(self):
        tail = verify_committed_prefix(self.directory / "index.jsonl", self.state["index"])
        orphan = any(
            p.name != "index.jsonl"
            and (not p.stem.isdecimal() or int(p.stem) >= int(self.state["sequence"]))
            for p in self.directory.iterdir()
        )
        return tail or orphan

    def _rows(self, channel, probe, start, end, level):
        key = f"{channel}/{probe}"
        if key not in self.state["pending"]:
            raise ValueError("unknown recorded probe or channel")
        for row in _entries(self.directory, self.state):
            size = 1 if level == "raw" else self.policy.base * self.policy.factor**level
            candidate_start = max(0, row["start"] - size + 1)
            if row["end"] <= start or candidate_start >= end:
                continue
            path = _shard(self.directory, row)
            with h5py.File(path, "r") as file:
                if file.attrs["header"] != json.dumps(self.state["header"], sort_keys=True) or (
                    file.attrs["start"],
                    file.attrs["end"],
                ) != (str(row["start"]), str(row["end"])):
                    raise ValueError("probe shard binding differs")
                group = file[key]
                if level == "raw":
                    data = group["raw"]
                    n = row["end"] - row["start"]
                    if data.shape != (n,) or data.dtype != np.dtype("<f8"):
                        raise ValueError("raw probe dataset shape differs")
                    lo, hi = max(start, row["start"]), min(end, row["end"])
                    for first in range(lo, hi, 4096):
                        values = data[first - row["start"] : min(hi, first + 4096) - row["start"]]
                        for offset, val in enumerate(values):
                            if not np.isfinite(val):
                                raise ValueError("nonfinite raw probe")
                            yield [
                                first + offset,
                                1,
                                float(val),
                                float(val),
                                float(val),
                                float(val),
                            ]
                elif str(level) in group:
                    data = group[str(level)]
                    n = len(data["start"])
                    if (
                        n > MAX_CHUNK // self.policy.base + 1
                        or data["count"].shape != (n,)
                        or data["values"].shape != (n, 4)
                    ):
                        raise ValueError("probe summary shape exceeds chunk budget")
                    size = self.policy.base * self.policy.factor**level
                    for first in range(0, n, 4096):
                        starts = data["start"][first : first + 4096]
                        counts = data["count"][first : first + 4096]
                        values = data["values"][first : first + 4096]
                        for lo, count, vals in zip(starts, counts, values, strict=True):
                            lo, count = int(lo), int(count)
                            if (
                                lo % size
                                or count != size
                                or lo + count > row["end"]
                                or lo + count <= row["start"]
                                or not np.isfinite(vals).all()
                            ):
                                raise ValueError("probe summary boundary differs")
                            if lo < end and lo + count > start:
                                yield [lo, count, *map(float, vals)]
            _shard(self.directory, row)
        if level != "raw":
            tail = None
            for i in range(level + 1):
                pending = self.state["pending"][key][i]
                if pending is not None:
                    tail = _merge(pending, tail) if tail is not None else pending
            if tail is not None and tail[0] < end and tail[0] + tail[1] > start:
                yield tail

    def query(self, channel: int, probe: str, start: int, end: int, width: int) -> dict:
        validate_window(start, end, width)
        if f"{channel}/{probe}" not in self.state["pending"]:
            raise ValueError("unknown recorded probe or channel")
        start, end = max(0, start), min(self.step, end)
        if end <= start:
            return dict(status="not_recorded", reason="outside recorded probe span")
        common = dict(
            visibility="ORACLE_ONLY",
            origin="SIMULATED",
            derivation="recorded",
            source="committed research probe shards",
            display_interpolation="none",
        )
        span = end - start
        if self.policy.keep_raw:
            level, block = "raw", 1 if span <= 2 * width else -(-span // width)
            lo, hi = start, end
        else:
            if span < self.policy.base * width:
                return dict(status="not_recorded", reason="finer than persisted summary; replay it")
            level, size = 0, self.policy.base
            while level + 1 < LEVELS and size * self.policy.factor <= span // width:
                level, size = level + 1, size * self.policy.factor
            lo, hi = start // size * size, min(self.step, -(-end // size) * size)
            row_count = -(-(hi - lo) // size)
            block = size * max(1, -(-row_count // width))
        # Bucket numbering follows actual support, not the requested edge.
        buckets = {}
        for row in self._rows(channel, probe, lo, hi, level):
            bucket = (row[0] - lo) // block
            buckets[bucket] = _merge(buckets.get(bucket), row)
        verify_committed_prefix(self.directory / "index.jsonl", self.state["index"])
        ordered = [buckets[k] for k in sorted(buckets)]
        if not ordered or sum(r[1] for r in ordered) != hi - lo:
            raise ValueError("recorded probe query has missing or duplicate support")
        if len(ordered) > (2 * width if level == "raw" and block == 1 else width):
            raise ValueError("probe display budget exceeded")
        raw = level == "raw" and block == 1
        return dict(
            **common,
            status="exact_record" if raw else "summary_only",
            level="raw" if raw else "display_bins",
            start=lo,
            first_step=lo,
            block_size=block,
            block_steps=block,
            source_count=hi - lo,
            support_start=lo,
            support_end=hi,
            boundary_extra=lo < start or hi > end,
            values=[r[4] for r in ordered] if raw else [r[2:] + [r[1]] for r in ordered],
            columns=["min", "max", "first", "last", "count"],
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass
