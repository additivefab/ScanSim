"""Real producer exclusion must coexist with live reads of committed bytes."""

import hashlib
import subprocess
import sys

import pytest

from zlscan.evidence.legacy_runner import b_own_test_args, run_python
from zlscan.io.committed import CommittedFile


def test_live_prefix_is_readable_and_second_process_is_excluded(tmp_path):
    path = tmp_path / "capture"
    writer = CommittedFile(path)
    try:
        writer.append(b"committed prefix")
        cursor = writer.checkpoint()
        script = """
import json,sys
from pathlib import Path
from zlscan.io.committed import CommittedFile
p=Path(sys.argv[1]); cursor=json.loads(sys.argv[2])
assert p.read_bytes()==b'committed prefix'
try:
    w=CommittedFile.resume(p,cursor)
except OSError:
    sys.exit(0)
w.close()
sys.exit(9)
"""
        import json

        result = subprocess.run(
            [sys.executable, "-c", script, str(path), json.dumps(cursor)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
    finally:
        writer.close()
    resumed = CommittedFile.resume(path, cursor)
    try:
        resumed.append(b" + resumed")
        assert (
            resumed.checkpoint()["prefix_sha256"]
            == hashlib.sha256(b"committed prefix + resumed").hexdigest()
        )
    finally:
        resumed.close()
    assert path.read_bytes() == b"committed prefix + resumed"


def test_npz_adapter_retains_original_values_without_open_disk_handle(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    program = """
import unittest,tempfile
from pathlib import Path
import numpy as np
class Original(unittest.TestCase):
    def test_original_values(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'original.npz'
            np.savez(p, a=np.arange(5))
            loaded=np.load(p)
            p.unlink()
            np.testing.assert_array_equal(loaded['a'],np.arange(5))
"""
    original = tests / "test_original.py"
    original.write_text(program, encoding="utf-8")
    result = run_python(b_own_test_args(windows=True), tmp_path)
    assert result.returncode == 0, result.output
    assert "NPZ byte-snapshot I/O adapter" in result.output
    assert original.read_text(encoding="utf-8") == program


def test_writer_rejects_reserved_lock_region_without_writing(tmp_path):
    from zlscan.io.committed import WRITER_LEASE_OFFSET

    writer = CommittedFile(tmp_path / "large")
    try:
        writer.offset = WRITER_LEASE_OFFSET
        writer.check = lambda: None
        with pytest.raises(ValueError, match="lease"):
            writer.append(b"x")
        assert writer.path.stat().st_size == 0
    finally:
        writer.close()
