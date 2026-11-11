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


@pytest.mark.parametrize("name", sorted(PLANNED))
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
        [sys.executable, "-m", "zlscan", "run"], capture_output=True, text=True, check=False
    )
    assert result.returncode == NOT_IMPLEMENTED_EXIT_CODE
    assert "尚未实现" in result.stderr
