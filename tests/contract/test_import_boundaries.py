"""Static check of docs/dev/02_architecture.md, section 3: who may import whom.

It parses every module under src/zlscan with ``ast`` and compares its ``zlscan.*`` imports with
an allow-list per package. This is what makes "the reconstruction cannot see the truth" a rule a
test can fail, rather than a promise.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "zlscan"

# package -> top-level zlscan packages it may import (itself is always allowed).
# Packages not listed are unconstrained.
ALLOWED = {
    "contracts": set(),
    "truth": {"contracts", "engine", "config"},
    "calib": {"contracts", "io", "config", "hardware", "evidence"},
    "recon": {"contracts", "io", "config", "hardware", "evidence"},
    "telemetry": {"contracts", "engine", "truth"},
    "trace": {"contracts", "engine", "truth"},
    "replay": {"contracts", "engine", "truth", "trace"},
    "causality": {"contracts", "engine", "truth"},
    "api": {"telemetry", "trace", "replay", "causality", "io", "contracts"},
}
# Must never be imported by these packages, whatever the allow-list says (clearer failure message).
NEVER_FOR_ANALYSIS = {"truth", "metrics", "telemetry", "trace", "replay", "causality", "api"}


def zlscan_imports(source: str) -> set[str]:
    """Top-level zlscan packages imported by a piece of source (``import`` and ``from``)."""
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module]
        else:
            continue
        for name in names:
            parts = name.split(".")
            if parts[0] == "zlscan" and len(parts) > 1:
                found.add(parts[1])
    return found


def violations(package: str, source: str) -> set[str]:
    if package not in ALLOWED:
        return set()
    return zlscan_imports(source) - ALLOWED[package] - {package}


# ---- the checker itself ---------------------------------------------------------------------


def test_the_checker_sees_plain_and_from_imports():
    src = (
        "import zlscan.truth.afe\nfrom zlscan.metrics import x\n"
        "from zlscan import io\nimport numpy\n"
    )
    assert zlscan_imports(src) == {"truth", "metrics"}


def test_reconstruction_importing_the_truth_is_caught():
    assert violations("recon", "from zlscan.truth.scene import phantom\n") == {"truth"}
    assert violations("calib", "import zlscan.metrics.density\n") == {"metrics"}


def test_a_physics_module_importing_the_analysis_side_is_caught():
    assert violations("truth", "from zlscan.recon import reconstruct\n") == {"recon"}
    assert violations("truth", "from zlscan.contracts.time import ClockDomain\n") == set()


def test_contracts_may_not_import_anything_else_in_the_project():
    assert violations("contracts", "from zlscan.hardware import pins\n") == {"hardware"}


def test_the_analysis_never_list_is_a_subset_of_what_the_allow_lists_exclude():
    for package in ("calib", "recon"):
        assert not (ALLOWED[package] & NEVER_FOR_ANALYSIS)


# ---- the real tree --------------------------------------------------------------------------


def modules():
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC)
        if relative.parts[0] == "__init__.py":
            continue
        yield (relative.parts[0] if len(relative.parts) > 1 else ""), path


@pytest.mark.parametrize("package,path", list(modules()), ids=lambda v: str(v).split("zlscan/")[-1])
def test_no_module_breaks_the_import_boundaries(package, path):
    assert violations(package, path.read_text(encoding="utf-8")) == set(), (
        f"{path.relative_to(SRC)}"
    )


def test_the_checked_tree_is_not_empty():
    assert sum(1 for _ in modules()) > 20
