"""Committed original digital inputs and source-code/state fingerprints, not inferred events."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from zlscan.contracts.time import ClockDomain
from zlscan.io.committed import CommittedFile, verify_committed_prefix

MAX_ROW = 256 << 10
MAX_STEPS = 1_000_000


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _sha(value):
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def _integer(value, *, signed=False):
    if not isinstance(value, str):
        raise ValueError("producer history integer must be a decimal string")
    try:
        number = int(value)
    except ValueError as exc:
        raise ValueError("producer history integer must be a decimal string") from exc
    low, high = (-(2**63), 2**63) if signed else (0, 2**64)
    if str(number) != value or not low <= number < high:
        raise ValueError("producer history integer is not canonical or exceeds range")
    return number


def describe_source(source):
    words = np.ascontiguousarray(source.codes, dtype="<u2")
    return dict(
        first_index=str(source.first_index),
        count=len(source),
        epoch=source.epoch,
        codes_sha256=hashlib.sha256(memoryview(words).cast("B")).hexdigest(),
    )


def validate_checkpoint(state):
    value = json.loads(json.dumps(state, allow_nan=False))
    if (
        value.pop("checksum", None) != _hash(value)
        or value.get("format") != "zlscan-producer-history-checkpoint/1"
    ):
        raise ValueError("producer history checksum/version differs")
    header = value["header"]
    if (
        header.get("format") != "zlscan-producer-history/1"
        or header.get("origin") != "SIMULATED"
        or header.get("visibility") != "ORACLE_ONLY"
        or type(header["os"]) is not int
        or header["os"] < 2
        or not _sha(header["digital_config_hash"])
        or not _sha(header["initial_pipeline_hash"])
        or not _sha(value["after"])
        or type(value["closed"]) is not bool
        or not 0 <= _integer(value["step"]) < 2**64
        or not 0 <= _integer(value["records"]) < 2**64
    ):
        raise ValueError("producer history header or counters differ")
    clock = header["fine_clock"]
    channels, identity = header["channels"], header["identity"]
    if (
        not isinstance(channels, list)
        or not channels
        or any(type(c) is not int or not 0 <= c < 16 for c in channels)
        or channels != sorted(set(channels))
        or type(header["bits"]) is not int
        or not 1 <= header["bits"] <= 16
        or set(identity) != {"run_id", "branch_id", "session_id"}
        or any(not isinstance(identity[k], str) for k in ("run_id", "branch_id"))
        or type(identity["session_id"]) is not int
        or not 0 <= identity["session_id"] < 2**64
        or set(clock) != {"id", "freq_num", "freq_den", "phase_ticks"}
        or any(type(v) is not int for v in clock.values())
    ):
        raise ValueError("producer history source identity or clock differs")
    ClockDomain(**clock)
    return value


def _operation(row, step, closed, header):
    if closed or _integer(row["step"]) != step or not _sha(row["after"]):
        raise ValueError("producer operation rewrites a closed boundary")
    kind = row["op"]
    if kind == "sync":
        if (
            set(row) != {"op", "step", "ticks", "after"}
            or not isinstance(row["ticks"], list)
            or len(row["ticks"]) > 4096
            or any(
                _integer(t, signed=True) < ClockDomain(**header["fine_clock"]).tick_of(step)
                for t in row["ticks"]
            )
        ):
            raise ValueError("producer sync declaration differs")
    elif kind == "advance":
        end = _integer(row["end"])
        if (
            set(row) != {"op", "step", "end", "sources", "after"}
            or not step < end < 2**64
            or end - step > MAX_STEPS
        ):
            raise ValueError("producer advance boundary exceeds budget")
        source = -(-step // header["os"])
        if not isinstance(row["sources"], list) or len(row["sources"]) > 4096:
            raise ValueError("producer source group budget exceeded")
        for part in row["sources"]:
            if (
                set(part) != {"first_index", "count", "epoch", "codes_sha256"}
                or _integer(part["first_index"]) != source
                or type(part["count"]) is not int
                or part["count"] <= 0
                or type(part["epoch"]) is not int
                or not 0 <= part["epoch"] < 2**32
                or not _sha(part["codes_sha256"])
            ):
                raise ValueError("producer source coverage or identity differs")
            source += part["count"]
        if source != -(-end // header["os"]):
            raise ValueError("producer source coverage differs from physical interval")
        step = end
    elif kind == "finish":
        if set(row) != {"op", "step", "after"}:
            raise ValueError("producer EOF declaration differs")
        closed = True
    else:
        raise ValueError("unknown producer history operation")
    return step, closed, row["after"]


class HistoryWriter:
    def __init__(self, path: Path, header: dict):
        self.header = json.loads(json.dumps(header))
        self.step = self.records = 0
        self.closed = False
        self.after = header["initial_pipeline_hash"]
        # Validate before creating output.
        value = self._state(dict(offset="0", prefix_sha256=hashlib.sha256(b"").hexdigest()))
        validate_checkpoint(value)
        body = (json.dumps(self.header, sort_keys=True) + "\n").encode()
        if len(body) > 65536:
            raise ValueError("producer history header exceeds budget")
        self.file = CommittedFile(path)
        try:
            self.file.append(body)
        except BaseException:
            self.file.close()
            raise

    def _state(self, cursor):
        value = dict(
            format="zlscan-producer-history-checkpoint/1",
            header=self.header,
            step=str(self.step),
            records=str(self.records),
            closed=self.closed,
            after=self.after,
            file=cursor,
        )
        value["checksum"] = _hash(value)
        return value

    def _append(self, row):
        step, closed, after = _operation(row, self.step, self.closed, self.header)
        body = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
        if len(body) > MAX_ROW:
            raise ValueError("producer operation row exceeds budget")
        self.file.append(body)
        self.step, self.closed, self.after = step, closed, after
        self.records += 1

    def sync(self, ticks, after):
        self._append(
            dict(op="sync", step=str(self.step), ticks=[str(t) for t in ticks], after=after)
        )

    def advance(self, start, end, sources, after):
        self._append(
            dict(op="advance", step=str(start), end=str(end), sources=sources, after=after)
        )

    def finish(self, after):
        self._append(dict(op="finish", step=str(self.step), after=after))

    def checkpoint(self):
        return self._state(self.file.checkpoint())

    @classmethod
    def restore(cls, path, state, *, branch_path=None):
        reader = HistoryReader(path, state)
        value = reader.state
        result = cls.__new__(cls)
        result.header, result.step = value["header"], int(value["step"])
        result.records, result.closed, result.after = (
            int(value["records"]),
            value["closed"],
            value["after"],
        )
        result.file = CommittedFile.resume(path, value["file"], branch_path=branch_path)
        return result

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.file.close()


class HistoryReader:
    def __init__(self, path, state):
        self.path, self.state = Path(path), validate_checkpoint(state)
        self.header = self.state["header"]
        self.unchanged()
        step = records = 0
        closed, after = False, self.header["initial_pipeline_hash"]
        for row in self.after(None):
            step, closed, after = _operation(row, step, closed, self.header)
            records += 1
        if (step, records, closed, after) != (
            int(self.state["step"]),
            int(self.state["records"]),
            self.state["closed"],
            self.state["after"],
        ):
            raise ValueError("producer history prefix and checkpoint differ")
        self.unchanged()

    def unchanged(self):
        return verify_committed_prefix(self.path, self.state["file"])

    def after(self, cursor):
        offset = None
        if cursor is not None:
            value = validate_checkpoint(cursor)
            if value["header"] != self.header or int(value["file"]["offset"]) > int(
                self.state["file"]["offset"]
            ):
                raise ValueError("historical producer cursor exceeds committed prefix")
            # A cursor must describe an actual complete prefix, including its row count/boundary.
            HistoryReader(self.path, cursor)
            offset = int(value["file"]["offset"])
        self.unchanged()
        with self.path.open("rb") as stream:
            limit = int(self.state["file"]["offset"])
            first = stream.readline(min(limit, 65536))
            if not first.endswith(b"\n") or json.loads(first) != self.header:
                raise ValueError("producer history header differs")
            if offset is not None:
                stream.seek(offset)
            while stream.tell() < limit:
                body = stream.readline(min(limit - stream.tell(), MAX_ROW + 1))
                if len(body) > MAX_ROW or not body.endswith(b"\n"):
                    raise ValueError("producer history row exceeds budget or is incomplete")
                yield json.loads(body)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass
