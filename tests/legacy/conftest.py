"""Fixtures that run the frozen legacy packages once per session, in scratch copies."""

import importlib.util

import pytest

from zlscan.evidence import legacy_runner as lr


def pytest_collection_modifyitems(items):
    """Everything under tests/legacy is a legacy regression (``-m legacy`` selects it)."""
    for item in items:
        if "tests/legacy/" in item.nodeid.replace("\\", "/"):
            item.add_marker(pytest.mark.legacy)


@pytest.fixture(scope="session")
def staged(tmp_path_factory):
    root = tmp_path_factory.mktemp("legacy_stage")
    lr.stage_package("package_a", root)
    lr.stage_package("package_b", root)
    return root


@pytest.fixture(scope="session")
def a_demo(staged, tmp_path_factory):
    if importlib.util.find_spec("matplotlib") is None:
        pytest.fail('matplotlib is missing; install it with: pip install -e ".[dev,legacy]"')
    out = tmp_path_factory.mktemp("a_out") / "out"
    return lr.run_a_demo(staged / "package_a", out)


@pytest.fixture(scope="session", params=lr.B_CONFIGS)
def b_run(request, staged, tmp_path_factory):
    out = tmp_path_factory.mktemp("b_out") / "out"
    result, metrics = lr.run_b_sim(staged / "package_b", request.param, out)
    return request.param, result, metrics
