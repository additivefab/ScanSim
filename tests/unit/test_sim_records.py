"""Records for cases that could not be run say so plainly and never claim a result."""

import json
from datetime import UTC, datetime

import pytest

from zlscan.evidence import sim_tools as st

NOW = datetime(2026, 11, 11, 9, 0, 0, tzinfo=UTC)
CODE = {"commit": "b" * 40, "dirty": False}
ENV = {"python": "3.14.7"}


@pytest.fixture
def legacy(tmp_path):
    """A stand-in frozen tree containing every file the case table names."""
    root = tmp_path / "repo"
    for case in st.CASES:
        for relative in case.files:
            target = root / "legacy" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"content of {relative}\n", encoding="utf-8")
    return root


def test_one_not_executed_record_per_case_when_no_tool_exists(legacy):
    runs = legacy / "evidence_runs"
    paths = st.write_not_executed_records(
        runs_dir=runs, date="2026-11-11", repo_root=legacy, found={}, code=CODE,
        environment=ENV, now=NOW,
    )  # fmt: skip
    assert len(paths) == len(st.CASES)
    for case, path in zip(st.CASES, paths, strict=True):
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["conclusion"] == "not_executed"
        assert record["criteria"] == [] and record["commands"] == []
        assert record["name"] == f"sim_{case.name}"
        assert all(tool in record["notes"] for tool in case.tools)
        assert set(record["inputs"]) == {f"legacy/{f}" for f in case.files}
        assert all(len(h) == 64 for h in record["inputs"].values())
        assert "pass" not in record["conclusion"]


def test_the_log_lists_every_probed_tool(legacy):
    paths = st.write_not_executed_records(
        runs_dir=legacy / "evidence_runs", date="2026-11-11", repo_root=legacy, found={},
        code=CODE, environment=ENV, now=NOW,
    )  # fmt: skip
    log = (paths[0].parent / "log.txt").read_text(encoding="utf-8")
    for tool in ("iverilog", "vvp", "ngspice"):
        assert f"{tool}: not found" in log


def test_it_refuses_when_a_case_could_actually_run(legacy):
    found = {t: st.ToolInfo(t, f"/bin/{t}", "1") for c in st.CASES for t in c.tools}
    with pytest.raises(RuntimeError, match="run path"):
        st.write_not_executed_records(
            runs_dir=legacy / "evidence_runs", date="2026-11-11", repo_root=legacy,
            found=found, code=CODE, environment=ENV, now=NOW,
        )  # fmt: skip
    assert not (legacy / "evidence_runs").exists(), "nothing may be written when refusing"


def test_a_second_run_on_the_same_day_does_not_overwrite(legacy):
    kwargs = dict(
        runs_dir=legacy / "evidence_runs", date="2026-11-11", repo_root=legacy, found={},
        code=CODE, environment=ENV, now=NOW,
    )  # fmt: skip
    st.write_not_executed_records(**kwargs)
    with pytest.raises(FileExistsError):
        st.write_not_executed_records(**kwargs)
