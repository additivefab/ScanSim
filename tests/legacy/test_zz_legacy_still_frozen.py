"""Runs last (file name): running the legacy packages must not have changed legacy/."""

from zlscan.evidence.legacy_runner import LEGACY_ROOT
from zlscan.io.manifest import MANIFEST_NAME, load_manifest, verify_manifest


def test_legacy_is_unchanged_after_the_regression_runs():
    problems = verify_manifest(LEGACY_ROOT, load_manifest(LEGACY_ROOT / MANIFEST_NAME))
    assert problems == [], problems
