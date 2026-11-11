"""Evidence records (docs/dev/06_testing.md, section 7) must not be able to overstate a result."""

import json
import math
import subprocess
from datetime import UTC, datetime

import pytest

from zlscan.evidence.records import (
    FORMAT,
    compare,
    environment_info,
    git_state,
    write_record,
)

T0 = datetime(2026, 11, 11, 8, 0, 0, tzinfo=UTC)
T1 = datetime(2026, 11, 11, 8, 0, 5, tzinfo=UTC)
GOOD_CMD = {"argv": ["python", "x.py"], "cwd": "/w", "returncode": 0, "seconds": 1.0}
CODE = {"commit": "a" * 40, "dirty": False}


def record_kwargs(**over):
    base = dict(
        name="demo",
        status="independent_rerun",
        conclusion="pass",
        commands=[GOOD_CMD],
        inputs={"a.json": "0" * 64},
        seed=20261110,
        criteria=[compare("rmse", 0.5, 0.5)],
        started_at=T0,
        finished_at=T1,
        log_text="output\n",
        code=CODE,
        environment={"python": "3.14.7"},
    )
    base.update(over)
    return base


# ---- compare -------------------------------------------------------------------------------


def test_integers_are_compared_exactly():
    assert compare("n", 65536, 65536)["passed"] is True
    assert compare("n", 65536, 65537)["passed"] is False
    assert compare("n", 65536, 65536)["tolerance"] is None


def test_float_inside_and_outside_relative_tolerance():
    assert compare("x", 1.0, 1.0 + 5e-13, rel_tol=1e-12)["passed"] is True
    assert compare("x", 1.0, 1.0 + 5e-12, rel_tol=1e-12)["passed"] is False


def test_float_absolute_tolerance_and_recorded_tolerance():
    c = compare("x", 0.0, 1e-9, abs_tol=1e-6)
    assert c["passed"] is True
    assert c["tolerance"] == {"abs": 1e-6, "rel": 0.0}
    assert c["expected"] == 0.0 and c["actual"] == 1e-9


def test_zero_tolerance_float_means_exact():
    assert compare("x", 0.1, 0.1)["passed"] is True
    assert compare("x", 0.1, 0.1 + 1e-17 + 1e-16)["passed"] is False


def test_nan_never_passes():
    assert compare("x", 1.0, math.nan, rel_tol=1.0)["passed"] is False
    assert compare("x", math.nan, math.nan, rel_tol=1.0)["passed"] is False


# ---- write_record --------------------------------------------------------------------------


def test_record_and_log_are_written(tmp_path):
    directory = tmp_path / "2026-11-11_demo"
    write_record(directory, **record_kwargs())
    record = json.loads((directory / "record.json").read_text(encoding="utf-8"))
    assert record["format"] == FORMAT
    assert record["status"] == "independent_rerun"
    assert record["conclusion"] == "pass"
    assert record["code"] == CODE
    assert record["seed"] == 20261110
    assert record["inputs"] == {"a.json": "0" * 64}
    assert record["criteria"][0]["quantity"] == "rmse"
    assert record["started_at"] == "2026-11-11T08:00:00+00:00"
    assert (directory / "log.txt").read_text(encoding="utf-8") == "output\n"


def test_an_existing_record_is_never_overwritten(tmp_path):
    directory = tmp_path / "r"
    write_record(directory, **record_kwargs())
    with pytest.raises(FileExistsError):
        write_record(directory, **record_kwargs())


@pytest.mark.parametrize("field,value", [("status", "verified"), ("conclusion", "passed")])
def test_unknown_status_or_conclusion_is_rejected(tmp_path, field, value):
    with pytest.raises(ValueError, match=field):
        write_record(tmp_path / "r", **record_kwargs(**{field: value}))


def test_pass_is_refused_when_a_criterion_failed(tmp_path):
    bad = [compare("rmse", 0.5, 0.6)]
    with pytest.raises(ValueError, match="criteri"):
        write_record(tmp_path / "r", **record_kwargs(criteria=bad))


def test_pass_is_refused_when_a_command_failed(tmp_path):
    failed = dict(GOOD_CMD, returncode=1)
    with pytest.raises(ValueError, match="command"):
        write_record(tmp_path / "r", **record_kwargs(commands=[failed]))


def test_a_failing_run_can_be_recorded_as_fail(tmp_path):
    bad = [compare("rmse", 0.5, 0.6)]
    write_record(tmp_path / "r", **record_kwargs(criteria=bad, conclusion="fail"))
    assert (tmp_path / "r" / "record.json").is_file()


@pytest.mark.parametrize(
    "missing",
    [
        {"commands": []},
        {"inputs": {}},
        {"code": {"commit": None, "dirty": None}},
        {"code": None},
    ],
)
def test_an_independent_rerun_needs_a_complete_record(tmp_path, missing):
    with pytest.raises(ValueError, match="independent_rerun"):
        write_record(tmp_path / "r", **record_kwargs(**missing))


def test_a_failing_independent_rerun_needs_a_complete_record_too(tmp_path):
    bad = [compare("rmse", 0.5, 0.6)]
    with pytest.raises(ValueError, match="independent_rerun"):
        write_record(tmp_path / "r", **record_kwargs(conclusion="fail", criteria=bad, code=None))


def test_author_reported_results_may_lack_a_commit(tmp_path):
    write_record(
        tmp_path / "r",
        **record_kwargs(status="author_reported", commands=[], inputs={}, code=None),
    )
    assert (tmp_path / "r" / "record.json").is_file()


def test_not_executed_needs_no_commands(tmp_path):
    write_record(
        tmp_path / "r",
        **record_kwargs(
            conclusion="not_executed", commands=[], criteria=[], notes="tool not installed"
        ),
    )
    record = json.loads((tmp_path / "r" / "record.json").read_text(encoding="utf-8"))
    assert record["conclusion"] == "not_executed"
    assert record["notes"] == "tool not installed"


# ---- environment and git -------------------------------------------------------------------


def test_environment_info_names_interpreter_and_numerics():
    info = environment_info()
    assert info["python"].count(".") == 2
    assert "numpy" in info["packages"] and "scipy" in info["packages"]
    assert info["platform"]


def test_git_state_outside_a_repository_is_unknown(tmp_path):
    assert git_state(tmp_path) == {"commit": None, "dirty": None}


def test_git_state_reports_commit_and_dirtiness(tmp_path):
    def git(*args):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    (tmp_path / "f.txt").write_text("1", encoding="utf-8")
    git("add", "f.txt")
    git("commit", "-q", "-m", "first")
    clean = git_state(tmp_path)
    assert len(clean["commit"]) == 40 and clean["dirty"] is False
    (tmp_path / "f.txt").write_text("2", encoding="utf-8")
    assert git_state(tmp_path)["dirty"] is True
