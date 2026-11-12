"""Disk-indexed, bounded views of a frozen multi-channel ZLRAW committed prefix."""

from __future__ import annotations

import json
import sqlite3
import struct
import tempfile
from pathlib import Path

import numpy as np

from zlscan.io.committed import verify_committed_prefix
from zlscan.io.observable_arrays import decode_sample
from zlscan.io.rawstream import _HEAD, BLOCK_HEADER_SIZE, PRELUDE, _channels, verify_raw_prefix
from zlscan.trace.window import validate_window


def _key(value):
    if type(value) is not int or not 0 <= value <= 2**64:
        raise ValueError("source sample index outside uint64 span")
    return f"{value:020d}"


class CommittedADC:
    def __init__(
        self, path: Path, writer: dict, length: int, channels: tuple[int, ...], domain: int
    ):
        self.path, self.writer, self.length, self.channels = path, writer, length, channels
        self.temporary = tempfile.TemporaryDirectory(prefix="zlscan-committed-adc-")
        self.database = Path(self.temporary.name) / "blocks.sqlite"
        connection = None
        try:
            self.unchanged()
            connection = sqlite3.connect(self.database)
            connection.execute("PRAGMA cache_size=-2048")
            connection.execute("PRAGMA temp_store=FILE")
            connection.execute(
                "CREATE TABLE blocks(channel INTEGER, epoch INTEGER, "
                "lo TEXT, hi TEXT, first TEXT, count INTEGER, payload INTEGER, stride INTEGER, "
                "col INTEGER, seq INTEGER, line TEXT, flags INTEGER)"
            )
            self.block_count = self.delivered_rows = 0
            with path.open("rb") as stream:
                self.session = verify_raw_prefix(
                    stream,
                    int(writer["file"]["offset"]),
                    writer["session_hash"],
                    int(writer["next_sequence"]),
                )
                if self.session.origin != "SIMULATED" or self.session.coding != "offset_binary":
                    raise ValueError("recording display requires decoded SIMULATED codes")
                stream.seek(len(PRELUDE))
                size = struct.unpack("<I", stream.read(4))[0]
                stream.seek(size, 1)
                previous = 0
                while stream.tell() < int(writer["file"]["offset"]):
                    fields = _HEAD.unpack(stream.read(BLOCK_HEADER_SIZE)[:60])
                    (
                        _,
                        _,
                        _,
                        _,
                        epoch,
                        seq,
                        mask,
                        clock,
                        flags,
                        _,
                        first,
                        count,
                        payload,
                        line,
                        _,
                        _,
                    ) = fields
                    present = tuple(_channels(mask))
                    if (
                        present != channels
                        or clock != domain
                        or first < previous
                        or first + count > length
                    ):
                        raise ValueError("recording block identities exceed frozen source span")
                    position = stream.tell()
                    for col, channel in enumerate(present):
                        connection.execute(
                            "INSERT INTO blocks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                channel,
                                epoch,
                                _key(first),
                                _key(first + count),
                                str(first),
                                count,
                                position,
                                len(present),
                                col,
                                seq,
                                str(line),
                                flags,
                            ),
                        )
                    previous = first + count
                    stream.seek(payload, 1)
                    self.block_count += 1
                    self.delivered_rows += count
            connection.execute("CREATE INDEX query_blocks ON blocks(channel,hi)")
            connection.execute("CREATE INDEX sequence_blocks ON blocks(seq)")
            connection.commit()
            self.epochs = [
                r[0]
                for r in connection.execute(
                    "SELECT DISTINCT epoch FROM blocks ORDER BY epoch LIMIT 4097"
                )
            ]
            if len(self.epochs) > 4096:
                raise ValueError("recording epoch index exceeds software budget")
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
        return verify_committed_prefix(self.path, self.writer["file"])

    def _connection(self):
        connection = sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)
        connection.execute("PRAGMA cache_size=-2048")
        return connection

    def rows(self, channel, start, end):
        if channel not in self.channels:
            raise ValueError("unknown recorded channel")
        connection = self._connection()
        try:
            cursor = connection.execute(
                "SELECT * FROM blocks WHERE channel=? AND hi>? ORDER BY hi", (channel, _key(start))
            )
            for row in cursor:
                if int(row[4]) >= end:
                    break
                yield row
        finally:
            connection.close()

    def query(self, channel, start, end, width):
        validate_window(start, end, width)
        self.unchanged()
        start, end = max(0, start), min(self.length, end)
        if end <= start:
            return dict(status="not_recorded", values=[], reason="no ADC sample in window")
        count = end - start
        block = 1 if count <= 2 * width else -(-count // width)
        size = -(-count // block)
        lows, highs, firsts, lasts = ([None] * size for _ in range(4))
        recorded = [0] * size
        epochs = [None] * size
        mixed = [False] * size
        with self.path.open("rb") as stream:
            for row in self.rows(channel, start, end):
                _, epoch, _, _, first, n, payload, stride, col, _, _, _ = row
                first = int(first)
                lo = max(first, start)
                hi = min(first + n, end)
                for a in range(lo, hi, 4096):
                    stop = min(hi, a + 4096)
                    stream.seek(payload + (a - first) * stride * 2)
                    body = stream.read((stop - a) * stride * 2)
                    if len(body) != (stop - a) * stride * 2:
                        raise ValueError("committed ADC payload truncated")
                    codes = np.frombuffer(body, dtype="<u2").reshape(-1, stride)[:, col]
                    for bucket in range((a - start) // block, -(-(stop - start) // block)):
                        edge = start + bucket * block
                        left, right = max(a, edge), min(stop, edge + block)
                        piece = codes[left - a : right - a]
                        low, high = int(piece.min()), int(piece.max())
                        lows[bucket] = low if lows[bucket] is None else min(low, lows[bucket])
                        highs[bucket] = high if highs[bucket] is None else max(high, highs[bucket])
                        if left == edge:
                            firsts[bucket] = int(piece[0])
                        if right == min(end, edge + block):
                            lasts[bucket] = int(piece[-1])
                        if recorded[bucket] and epochs[bucket] != epoch:
                            mixed[bucket] = True
                        if not mixed[bucket]:
                            epochs[bucket] = epoch
                        else:
                            epochs[bucket] = None
                        recorded[bucket] += right - left
        self.unchanged()
        return dict(
            status="exact_record" if block == 1 else "summary_only",
            level="raw" if block == 1 else "display_bins",
            start=start,
            block_size=block,
            source_count=count,
            support_start=start,
            support_end=end,
            boundary_extra=False,
            values=firsts
            if block == 1
            else [
                [lows[i], highs[i], firsts[i], lasts[i], min(block, count - i * block)]
                for i in range(size)
            ],
            columns=["min", "max", "first", "last", "count"],
            recorded_counts=recorded,
            missing_count=count - sum(recorded),
            epochs=epochs,
            mixed_epoch=mixed,
            visibility="OBSERVABLE",
            source="committed delivered ZLRAW only; unkept/lost positions stay null",
        )

    def sample(self, channel, index):
        self.unchanged()
        for row in self.rows(channel, index, index + 1):
            _, epoch, _, _, first, count, payload, stride, col, seq, line, flags = row
            first = int(first)
            if first <= index < first + count:
                with self.path.open("rb") as stream:
                    stream.seek(payload + (index - first) * stride * 2 + col * 2)
                    value = struct.unpack("<H", stream.read(2))[0]
                self.unchanged()
                return dict(
                    epoch=epoch,
                    first_sample_index=str(first),
                    sample_count=count,
                    block_sequence=seq,
                    line_number=line,
                    flags=flags,
                    value=value,
                )
        return None

    def events(self, channel, start, end, limit=4096):
        output = []
        previous = None
        for row in self.rows(channel, start, end):
            if int(row[4]) < start:
                previous = row[1]
                continue
            if row[1] != previous:
                output.append(
                    dict(
                        kind="epoch_observed",
                        sample_index=row[4],
                        epoch=row[1],
                        visibility="OBSERVABLE",
                        source="delivered block header",
                    )
                )
                previous = row[1]
                if len(output) >= limit:
                    break
        return output

    def add_journal(self, path, cursor):
        verify_committed_prefix(path, cursor)
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "CREATE TABLE deliveries(seq INTEGER PRIMARY KEY, tick TEXT, "
                "observables TEXT, count INTEGER)"
            )
            connection.execute("CREATE TABLE losses(lo TEXT, hi TEXT, body TEXT)")
            with path.open("rb") as stream:
                limit = int(cursor["offset"])
                while stream.tell() < limit:
                    line = stream.readline(limit - stream.tell())
                    if not line.endswith(b"\n"):
                        raise ValueError("committed journal row incomplete")
                    record = json.loads(line)
                    for entry in record.get("delivered", []):
                        seq = int(entry["block_sequence"])
                        block = connection.execute(
                            "SELECT first,count,epoch,payload,stride "
                            "FROM blocks WHERE seq=? LIMIT 1",
                            (seq,),
                        ).fetchone()
                        src = entry["source"]
                        if (
                            block is None
                            or (int(block[0]), block[1], block[2])
                            != (int(src["first_sample_index"]), src["count"], src["epoch"])
                            or int(entry["raw_offset"]) != block[3] - BLOCK_HEADER_SIZE
                            or int(entry["raw_end"]) != block[3] + block[1] * block[4] * 2
                        ):
                            raise ValueError("journal delivery differs from committed raw block")
                        connection.execute(
                            "INSERT INTO deliveries VALUES(?,?,?,?)",
                            (
                                seq,
                                entry["delivery_tick"],
                                json.dumps(src.get("observables", {})),
                                src["count"],
                            ),
                        )
                    for loss in record.get("losses", []):
                        lo, hi = int(loss["first_sample_index"]), int(loss["last_sample_index"]) + 1
                        if not 0 <= lo < hi <= self.length:
                            raise ValueError("loss exceeds committed source span")
                        connection.execute(
                            "INSERT INTO losses VALUES(?,?,?)",
                            (_key(lo), _key(hi), json.dumps(loss)),
                        )
            connection.execute("CREATE INDEX loss_query ON losses(hi)")
            count = connection.execute("SELECT count(*) FROM deliveries").fetchone()[0]
            if count != self.block_count:
                raise ValueError("journal delivery count differs from raw index")
            connection.commit()
            verify_committed_prefix(path, cursor)
        except sqlite3.IntegrityError as exc:
            raise ValueError("duplicate recorded journal delivery") from exc
        finally:
            connection.close()

    def losses(self, start, end, limit=4096):
        connection = self._connection()
        try:
            output = []
            for lo, _hi, body in connection.execute(
                "SELECT * FROM losses WHERE hi>? ORDER BY hi", (_key(start),)
            ):
                if int(lo) >= end:
                    break
                output.append(json.loads(body))
                if len(output) >= limit:
                    break
            return output
        finally:
            connection.close()

    def delivery(self, sequence):
        connection = self._connection()
        try:
            row = connection.execute(
                "SELECT tick FROM deliveries WHERE seq=?", (sequence,)
            ).fetchone()
            return row[0] if row else None
        finally:
            connection.close()

    def observables(self, sequence, index):
        connection = self._connection()
        try:
            row = connection.execute(
                "SELECT length(observables),count FROM deliveries WHERE seq=?", (sequence,)
            ).fetchone()
            if row is None:
                return dict(status="not_recorded", reason="observable vectors not registered")
            if row[0] > 8 * (1 << 20) or row[1] > 1_000_000:
                return dict(status="not_computed", reason="observable packet exceeds query budget")
            encoded = connection.execute(
                "SELECT observables FROM deliveries WHERE seq=?", (sequence,)
            ).fetchone()[0]
            values = decode_sample(json.loads(encoded), index, row[1])
            return dict(status="exact_record", values=values, visibility="OBSERVABLE")
        finally:
            connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.temporary.cleanup()
