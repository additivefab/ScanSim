"""The harness around the frozen packages: output parsing and non-destructive staging."""

import sys

import pytest

from zlscan.evidence import legacy_runner as lr


@pytest.mark.parametrize(
    "output,expected",
    [
        ("..............   [100%]\n14 passed in 2.13s\n", 14),
        ("1 failed, 13 passed in 1.0s\n", None),
        ("13 passed, 1 error in 1.0s\n", None),
        ("no summary here\n", None),
    ],
)
def test_parse_pytest_passed(output, expected):
    assert lr.parse_pytest_passed(output) == expected


@pytest.mark.parametrize(
    "output,expected",
    [
        ("....\n---\nRan 14 tests in 0.2s\n\nOK\n", 14),
        ("Ran 1 test in 0.1s\n\nOK\n", 1),
        ("Ran 14 tests in 0.2s\n\nFAILED (failures=1)\n", None),
        ("nothing\n", None),
    ],
)
def test_parse_unittest_ran(output, expected):
    assert lr.parse_unittest_ran(output) == expected


@pytest.fixture
def fake_legacy(tmp_path):
    root = tmp_path / "legacy"
    (root / "pkg" / "sub").mkdir(parents=True)
    (root / "pkg" / "sub" / "m.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "pkg" / "__pycache__").mkdir()
    (root / "pkg" / "__pycache__" / "x.pyc").write_bytes(b"x")
    return root


def test_staging_copies_the_package_but_not_bytecode(fake_legacy, tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    staged = lr.stage_package("pkg", scratch, legacy_root=fake_legacy)
    assert (staged / "sub" / "m.py").read_text(encoding="utf-8") == "print('hi')\n"
    assert not (staged / "__pycache__").exists()


def test_running_in_the_stage_does_not_touch_the_source(fake_legacy, tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    staged = lr.stage_package("pkg", scratch, legacy_root=fake_legacy)
    before = sorted(p.relative_to(fake_legacy).as_posix() for p in fake_legacy.rglob("*"))
    result = lr.run_python(["sub/m.py"], staged)
    assert result.returncode == 0 and result.output.strip() == "hi"
    assert result.argv[0] == sys.executable
    after = sorted(p.relative_to(fake_legacy).as_posix() for p in fake_legacy.rglob("*"))
    assert before == after


def test_run_python_reports_a_failure_instead_of_raising(fake_legacy, tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    staged = lr.stage_package("pkg", scratch, legacy_root=fake_legacy)
    (staged / "boom.py").write_text("raise SystemExit(7)\n", encoding="utf-8")
    result = lr.run_python(["boom.py"], staged)
    assert result.returncode == 7
    assert result.as_command()["returncode"] == 7


def test_delivered_metrics_are_read_from_the_frozen_tree():
    delivered = lr.delivered_metrics(lr.A_DELIVERED_METRICS)
    assert delivered["raw_active_sample_count"] == 65536
    assert set(lr.B_DELIVERED) == set(lr.B_CONFIGS)
