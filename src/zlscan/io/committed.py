"""Exclusive append files with verified, fsynced prefix cursors; no implicit truncation."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import BinaryIO


def _lock(stream: BinaryIO) -> None:
    try:
        if os.name == "nt":
            # Python's documented nonblocking byte-range lock may extend beyond EOF.
            import msvcrt

            position = stream.tell()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            stream.seek(position)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise OSError("cannot acquire exclusive output lock") from exc


class CommittedFile:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.stream = self.path.open("x+b", buffering=0)
        try:
            _lock(self.stream)
        except Exception:
            self.stream.close()
            raise
        self.offset = 0
        self.sha = hashlib.sha256()
        self.failed = False
        self._stamp = self._stat()

    def _stat(self) -> tuple:
        value = os.fstat(self.stream.fileno())
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns

    def check(self) -> None:
        if self.failed:
            raise OSError("output has failed; restore a committed checkpoint")
        current = self.path.stat()
        if (
            (current.st_dev, current.st_ino) != self._stamp[:2]
            or self._stat() != self._stamp
            or current.st_size != self.offset
        ):
            self.failed = True
            raise ValueError("output changed outside its locked writer")

    def append(self, data: bytes) -> None:
        self.check()
        try:
            self.stream.seek(self.offset)
            remaining = memoryview(data)
            while remaining:
                count = self.stream.write(remaining)
                if count is None or count <= 0:
                    raise OSError("output write made no progress")
                self.sha.update(remaining[:count])
                self.offset += count
                remaining = remaining[count:]
            self._stamp = self._stat()
        except Exception:
            self.failed = True
            raise

    def checkpoint(self) -> dict:
        self.check()
        try:
            self.stream.flush()
            os.fsync(self.stream.fileno())
        except OSError:
            self.failed = True
            raise
        self._stamp = self._stat()
        return {"offset": str(self.offset), "prefix_sha256": self.sha.hexdigest()}

    def close(self) -> None:
        self.stream.close()  # OS releases the exclusive lock with the handle.

    @classmethod
    def resume(cls, path: Path, cursor: dict, *, branch_path: Path | None = None) -> CommittedFile:
        offset = int(cursor["offset"])
        if offset < 0:
            raise ValueError("committed offset must be nonnegative")
        stream = Path(path).open("r+b", buffering=0)
        target = None
        transferred = False
        try:
            _lock(stream)
            initial = os.fstat(stream.fileno())
            if initial.st_size < offset:
                raise ValueError("file is shorter than its committed prefix")
            if branch_path is None and initial.st_size > offset:
                raise ValueError("uncommitted tail exists; choose a new branch path")
            digest = hashlib.sha256()
            remaining = offset
            while remaining:
                data = stream.read(min(1 << 20, remaining))
                if not data:
                    raise ValueError("committed prefix ended unexpectedly")
                digest.update(data)
                remaining -= len(data)
            if digest.hexdigest() != cursor["prefix_sha256"]:
                raise ValueError("committed prefix SHA-256 differs")
            after_hash = os.fstat(stream.fileno())
            if (initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns) != (
                after_hash.st_size,
                after_hash.st_mtime_ns,
                after_hash.st_ctime_ns,
            ):
                raise ValueError("committed source changed during verification")
            if branch_path is None:
                result = cls.__new__(cls)
                result.path, result.stream = Path(path), stream
                result.offset, result.sha, result.failed = offset, digest, False
                result._stamp = result._stat()
                result.check()
                transferred = True
                return result
            target = cls(branch_path)
            stream.seek(0)
            remaining = offset
            while remaining:
                data = stream.read(min(1 << 20, remaining))
                if not data:
                    raise ValueError("committed source changed during branch copy")
                target.append(data)
                remaining -= len(data)
            after = os.fstat(stream.fileno())
            if (initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ) or target.sha.hexdigest() != cursor["prefix_sha256"]:
                raise ValueError("committed source changed during branch copy")
            transferred = True
            return target
        finally:
            if not transferred and target is not None:
                target.close()
            if not transferred or target is not None:
                stream.close()


def publish_checkpoint(directory: Path, data: bytes) -> Path:
    """Publish one root after its caller has fsynced all referenced output prefixes."""
    path = directory / "checkpoint.json"
    with tempfile.NamedTemporaryFile(
        dir=directory, prefix="checkpoint-", suffix=".tmp", delete=False
    ) as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
        temporary = stream.name
    os.replace(temporary, path)
    if os.name != "nt":
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return path
