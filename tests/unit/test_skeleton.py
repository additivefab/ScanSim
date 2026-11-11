"""The package layout must match docs/dev/02_architecture.md, section 1."""

import importlib

import pytest

TOP_LEVEL = [
    "contracts", "evidence", "hardware", "config", "budget", "engine", "truth",
    "adc_link", "digital", "transport", "control", "experiment", "observation",
    "calib", "recon", "metrics", "ident", "telemetry", "trace", "replay",
    "causality", "api", "io", "report",
]  # fmt: skip

TRUTH = ["scene", "source", "scan", "psf", "collect", "detector", "afe", "adc", "timing"]


@pytest.mark.parametrize("name", TOP_LEVEL)
def test_top_level_package_imports(name):
    importlib.import_module(f"zlscan.{name}")


@pytest.mark.parametrize("name", TRUTH)
def test_truth_subpackage_imports(name):
    importlib.import_module(f"zlscan.truth.{name}")
