"""The CLI must expose every planned command and never succeed silently for unimplemented ones."""

import subprocess
import sys

import pytest

from zlscan import __version__
from zlscan.cli import COMMANDS, NOT_IMPLEMENTED_EXIT_CODE, USAGE_EXIT_CODE, main

# docs/dev/02_architecture.md, section 8.
PLANNED = {
    "budget", "check", "run", "sweep", "calibrate", "reconstruct", "metrics",
    "ident", "compare", "report", "cosim", "replay", "serve",
}  # fmt: skip


def test_command_table_matches_architecture_document():
    assert {c.name for c in COMMANDS} == PLANNED
    assert len(COMMANDS) == len(PLANNED), "duplicate command name"


def test_help_lists_every_command(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for name in PLANNED:
        assert name in out


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


# Commands that are implemented are tested in test_profile_and_check.py.
IMPLEMENTED = {"check", "budget", "run", "serve", "replay"}


@pytest.mark.parametrize("name", sorted(PLANNED - IMPLEMENTED))
def test_unimplemented_command_fails_loudly(name, capsys):
    code = main([name])
    assert code == NOT_IMPLEMENTED_EXIT_CODE
    assert code != 0
    assert "尚未实现" in capsys.readouterr().err


def test_no_command_is_a_usage_error():
    assert main([]) == USAGE_EXIT_CODE


def test_unknown_command_is_rejected():
    with pytest.raises(SystemExit) as exc:
        main(["no-such-command"])
    assert exc.value.code == USAGE_EXIT_CODE


def test_module_entry_point_runs_in_a_fresh_interpreter():
    result = subprocess.run(
        [sys.executable, "-m", "zlscan", "sweep"], capture_output=True, text=True, check=False
    )
    assert result.returncode == NOT_IMPLEMENTED_EXIT_CODE
    assert "尚未实现" in result.stderr


def test_cli_replays_a_saved_physical_window_without_mutating_source(tmp_path):
    import json

    import numpy as np

    source, output = tmp_path / "source", tmp_path / "replayed"
    assert main(["run", "--mode", "physical", "--lines", "3", "--out", str(source)]) == 0
    manifest = (source / "run.json").read_bytes()
    assert (
        main(
            [
                "replay",
                "--run",
                str(source),
                "--start-step",
                "11000",
                "--steps",
                "100",
                "--out",
                str(output),
            ]
        )
        == 0
    )
    report = json.loads((output / "run.json").read_text())
    assert report["checkpoint_step"] == 10000
    assert report["status"] == "replayed"
    assert (source / "run.json").read_bytes() == manifest
    originals = np.load(source / "truth/source_codes.npy")
    with np.load(output / "samples.npz") as replayed:
        np.testing.assert_array_equal(replayed["codes"], originals[2750:2775, 0])
    assert (
        main(
            [
                "replay",
                "--run",
                str(source),
                "--start-step",
                "29999",
                "--steps",
                "2",
                "--out",
                str(tmp_path / "invalid"),
            ]
        )
        == 1
    )
    assert not (tmp_path / "invalid").exists()
