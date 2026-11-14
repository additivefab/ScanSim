"""Quick MVP approximate path tests."""

import json

import numpy as np

from zlscan.quick.model import QuickConfig, run_quick


def test_quick_run_writes_reloadable_artifacts(tmp_path):
    result = run_quick(QuickConfig(width=32, height=16), tmp_path / "run")
    assert result.metadata["format"] == "zlscan.quick-run/1"
    assert result.metadata["fidelity"] == "APPROXIMATE"
    assert result.arrays["code"].shape == (16, 32)
    assert (result.run_dir / "preview.png").is_file()
    assert (
        (result.run_dir / "selected_line.csv")
        .read_text(encoding="utf-8")
        .startswith("quick_sample_index")
    )
    assert (
        json.loads((result.run_dir / "quick_run.json").read_text(encoding="utf-8"))["origin"]
        == "SIMULATED"
    )
    loaded = np.load(result.run_dir / "arrays.npz")
    np.testing.assert_array_equal(loaded["code"], result.arrays["code"])


def test_quick_controls_have_explainable_effects(tmp_path):
    ideal = run_quick(QuickConfig(width=48, height=24, spot_fwhm_m=0, tau_s=0), tmp_path / "ideal")
    blurred = run_quick(
        QuickConfig(width=48, height=24, spot_fwhm_m=0.004, tau_s=0), tmp_path / "blur"
    )
    noisy = run_quick(
        QuickConfig(width=48, height=24, noise_sigma_v=0.05, seed=7), tmp_path / "noise"
    )
    assert not np.array_equal(ideal.arrays["density_hat"], blurred.arrays["density_hat"])
    assert not np.array_equal(ideal.arrays["voltage_v"], noisy.arrays["voltage_v"])


def test_quick_rejects_invalid_geometry(tmp_path):
    cfg = QuickConfig(width=32, height=16, scene_width_m=0)
    try:
        run_quick(cfg, tmp_path / "bad")
    except ValueError as exc:
        assert "scene dimensions" in str(exc)
    else:
        raise AssertionError("invalid quick geometry was accepted")
