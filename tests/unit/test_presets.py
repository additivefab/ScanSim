"""Legacy presets: the steady-state codes of the cross-comparison, converters, explicit switches."""

import json
from pathlib import Path

import numpy as np
import pytest

from zlscan.config.presets import from_legacy_a, from_legacy_b, legacy_dc_config
from zlscan.engine.chain import Chain
from zlscan.engine.physical_run import line_steps
from zlscan.truth.scene import step_wedge

ROOT = Path(__file__).resolve().parents[2]
DATA = json.loads(
    (ROOT / "docs/reports/legacy_cross_compare_data.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("row", DATA["rows"], ids=lambda r: f"D{r['density']:g}")
def test_legacy_dc_codes_are_reproduced_exactly(row):
    out = Chain(legacy_dc_config(row["density"])).run_steps(2000, 2000)
    assert set(out.codes.tolist()) == {int(row["a"]["code"])} == {int(row["b"]["code"])}


def test_the_legacy_packages_configs_convert_and_list_what_is_dropped():
    a = json.loads((ROOT / "legacy/package_a/config/demo.json").read_text(encoding="utf-8"))
    b = json.loads((ROOT / "legacy/package_b/configs/demo_log.json").read_text(encoding="utf-8"))
    cfg_a, lost_a = from_legacy_a(a)
    cfg_b, lost_b = from_legacy_b(b)
    assert lost_a and lost_b
    assert cfg_a.fs_hz == 20_000_000 and cfg_a.scan.nx == 256 and cfg_a.legacy_reset_per_line
    assert cfg_b.fs_hz == 10_000_000 and cfg_b.afe.polarity == -1 and cfg_b.afe.v0 == 0.2


def test_package_b_converter_preserves_analogue_history_across_lines():
    legacy = json.loads((ROOT / "legacy/package_b/configs/demo_log.json").read_text())
    cfg, _ = from_legacy_b(legacy)
    assert cfg.afe.pole_hz > 0
    assert not cfg.legacy_reset_per_line


def test_legacy_reset_per_line_changes_the_result_and_default_does_not_reset():
    """When the scan fills the whole line the state at a line start is not a steady state."""
    from zlscan.config.presets import demo_config

    def make(reset):
        cfg = demo_config()
        cfg.detector = type(cfg.detector)(**{**cfg.detector.__dict__, "shot_noise": False})
        cfg.scan = type(cfg.scan)(
            **{**cfg.scan.__dict__, "nx": 157, "active_offset_s": 0.0, "x_center_m": 0.157}
        )
        cfg.scenes = {"main": step_wedge([0.2, 2.5], 0.314, 0.2)}
        cfg.source = type(cfg.source)(power_w=1e-3, gated=False)
        cfg.legacy_reset_per_line = reset
        return cfg

    steps = line_steps(make(False))
    continuous = Chain(make(False)).run_steps(3 * steps, steps).codes
    legacy = Chain(make(True)).run_steps(3 * steps, steps).codes
    assert not np.array_equal(continuous, legacy)
    assert not make(False).legacy_reset_per_line


def test_a_declared_response_reports_its_grid_error_and_does_not_validate_hardware():
    from zlscan.config.presets import demo_config
    from zlscan.engine.validity import physical_applicability

    cfg = demo_config()
    cfg.detector = type(cfg.detector)(**{**cfg.detector.__dict__, "pole_hz": 8e6})
    assert Chain(cfg).detector_response is not None
    applicability = physical_applicability(cfg)
    assert applicability["finite_grid_to_continuous_current_variance_ratio"] < 0.16
    assert applicability["high_density_capability"] == "not_validated"
