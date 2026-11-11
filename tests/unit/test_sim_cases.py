"""The table of RTL/SPICE cases must point at files that are really frozen in legacy/."""

from pathlib import Path

import pytest

from zlscan.evidence import sim_tools as st
from zlscan.io.manifest import MANIFEST_NAME, load_manifest

LEGACY = Path(__file__).resolve().parents[2] / "legacy"


@pytest.fixture(scope="module")
def frozen_paths():
    return {f["path"] for f in load_manifest(LEGACY / MANIFEST_NAME)["files"]}


def test_case_names_are_unique():
    names = [c.name for c in st.CASES]
    assert len(names) == len(set(names))


def test_every_case_file_is_in_the_frozen_manifest(frozen_paths):
    missing = [(c.name, f) for c in st.CASES for f in c.files if f not in frozen_paths]
    assert missing == []


def test_every_case_names_its_tools_and_kind():
    for case in st.CASES:
        assert case.kind in {"rtl", "spice", "vivado"}
        assert case.tools and all(case.tools)
        assert case.files


def test_all_shipped_testbenches_and_netlists_are_covered(frozen_paths):
    """Nothing the packages ship for RTL/SPICE may silently fall outside the table."""
    covered = {f for c in st.CASES for f in c.files}
    shipped = {
        p
        for p in frozen_paths
        if p.endswith((".cir",)) or (p.endswith(".sv") and "/rtl/" in p and "tb_" in p)
    }
    assert shipped <= covered, sorted(shipped - covered)


def test_rtl_cases_state_how_many_pass_lines_they_must_print():
    rtl = [c for c in st.CASES if c.kind == "rtl"]
    assert rtl and all(
        isinstance(c.expected_pass_lines, int) and c.expected_pass_lines > 0 for c in rtl
    )


def test_missing_tools_are_listed_by_name():
    case = next(c for c in st.CASES if c.kind == "spice")
    assert st.missing_tools(case, found={}) == list(case.tools)
    found = {t: st.ToolInfo(t, f"/bin/{t}", "x") for t in case.tools}
    assert st.missing_tools(case, found=found) == []
