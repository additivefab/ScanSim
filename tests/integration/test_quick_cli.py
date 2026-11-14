"""CLI smoke test for the independent Quick MVP path."""

import subprocess
import sys


def test_quick_cli_creates_run(tmp_path):
    out = tmp_path / "quick"
    completed = subprocess.run(
        [sys.executable, "-m", "zlscan.cli", "quick", "--preset", "demo80", "--out", str(out)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert (out / "quick_run.json").is_file()
    assert (out / "arrays.npz").is_file()
