"""legacy/ is read-only: it must match legacy/MANIFEST.json exactly (decision D001, D019)."""

from pathlib import Path

import pytest

from zlscan.io.manifest import MANIFEST_NAME, load_manifest, verify_manifest

ROOT = Path(__file__).resolve().parents[2]
LEGACY = ROOT / "legacy"
MAX_FILE_BYTES = 50 * 1024 * 1024  # D019: stop and report above this size


def test_legacy_directory_and_manifest_exist():
    assert LEGACY.is_dir(), "legacy/ is missing"
    assert (LEGACY / MANIFEST_NAME).is_file(), "legacy/MANIFEST.json is missing"


def test_legacy_matches_manifest_exactly():
    problems = verify_manifest(LEGACY, load_manifest(LEGACY / MANIFEST_NAME))
    assert problems == [], "legacy/ drifted from its manifest:\n" + "\n".join(problems)


def test_manifest_is_not_empty():
    assert len(load_manifest(LEGACY / MANIFEST_NAME)["files"]) > 100


@pytest.mark.parametrize(
    "relative",
    [
        "package_a/src/scanner_sim.py",
        "package_a/run_demo.py",
        "package_a/config/demo.json",
        "package_a/rtl/scanner_capture.sv",
        "package_a/MANIFEST_SHA256.txt",
        "package_b/src/scanner.py",
        "package_b/run_sim.py",
        "package_b/reconstruct_capture.py",
        "package_b/rtl/tag_delay.sv",
        "package_b/MANIFEST_SHA256.txt",
        "review/tables/pin_registry.json",
        "review/configs/硬件配置草案_未知项禁止自动补齐.json",
        "review/scripts/review_checks.py",
        "review/docs/项目资料审查记录.md",
        "originals/SmartZynq_SP2_Schematic_V1d3B_20260413.pdf.pdf",
        "originals/激光扫描仪调研学习记录.docx",
    ],
)
def test_expected_source_files_were_frozen(relative):
    assert (LEGACY / relative).is_file(), relative


def test_later_plan_documents_were_not_copied_into_legacy():
    """The plan documents are not legacy inputs; they live in docs/plan/."""
    stray = [p.name for p in LEGACY.rglob("仿真软件开发方案_*.md")]
    assert stray == [], stray


def test_the_new_project_was_not_copied_into_itself():
    assert not (LEGACY / "zlscan_v5").exists()
    assert not any(p.name == "pyproject.toml" and "zlscan" in str(p) for p in LEGACY.rglob("*"))


def test_no_single_file_exceeds_the_size_limit():
    big = [str(p.relative_to(LEGACY)) for p in LEGACY.rglob("*") if p.is_file()
           and p.stat().st_size > MAX_FILE_BYTES]  # fmt: skip
    assert big == [], big
