import json

import numpy as np
import pytest
from scipy.special import ndtr
from scipy.stats import exponnorm

from zlscan.cli import main
from zlscan.metrics.targets import evaluate_targets


def spec(tmp_path, evaluations, **arrays):
    for name, array in arrays.items():
        np.save(tmp_path / f"{name}.npy", array)
    path = tmp_path / "targets.json"
    path.write_text(json.dumps({"format": "zlscan-quality-targets/1", "evaluations": evaluations}))
    return path


def test_cli_wedge_uses_saved_common_mask_and_records_inputs(tmp_path):
    image = np.tile([0.1, 1.1, 2.1], (3, 1))
    flags = np.zeros(image.shape, np.uint16)
    flags[0, 2] = 32
    np.savez(tmp_path / "density.npz", density=image, flags=flags)
    np.save(tmp_path / "truth.npy", image - 0.1)
    optical = image - 0.1
    optical[1, 2] = np.nan
    np.save(tmp_path / "optical.npy", optical)
    path = spec(
        tmp_path,
        [
            {
                "name": "wedge",
                "kind": "wedge",
                "labels": "labels.npy",
                "levels_od": [0, 1, 2],
                "max_abs_bias_od": 0.11,
                "max_std_od": 0.01,
            }
        ],
        labels=np.tile(np.arange(3), (3, 1)),
    )
    out = tmp_path / "metrics"
    assert (
        main(
            [
                "metrics",
                "--image",
                str(tmp_path / "density.npz"),
                "--truth",
                str(tmp_path / "truth.npy"),
                "--optical",
                str(tmp_path / "optical.npy"),
                "--targets",
                str(path),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    result = json.loads((out / "run.json").read_text())
    wedge = result["target_metrics"]["wedge"]
    assert wedge["status"] == "computed"
    assert wedge["steps"][2]["pixels"] == 1
    assert wedge["steps"][0]["bias_od"] == pytest.approx(0.1)
    # A one-pixel high-density step cannot establish its noise or usability.
    assert wedge["usable_target_range_od"] == [0, 1]
    assert len(result["target_inputs"]) == 2
    with np.load(out / "mask.npz") as masks:
        np.testing.assert_array_equal(masks["valid"], masks["target_wedge"])
        assert not masks["target_wedge"][0, 2]
        assert not masks["target_wedge"][1, 2]


@pytest.mark.parametrize("direction", ["scan", "feed"])
def test_target_mtf_uses_transmittance_and_recovers_gaussian(tmp_path, direction):
    y, x = np.mgrid[0:128, 0:128]
    distance = (x - 64 - y / 8) * 1e-4
    sigma = 1.5e-4
    transmission = 0.2 + 0.7 * ndtr(distance / sigma)
    image = -np.log10(transmission)
    if direction == "feed":
        image, distance = image.T, distance.T
    path = spec(
        tmp_path,
        [
            {
                "name": "edge",
                "kind": "mtf",
                "direction": direction,
                "distance_m": "distance.npy",
                "region": "region.npy",
                "bin_width_m": 1.25e-5,
                "working_density_od": 0.5,
                "scan_speed_m_s": 1.0,
            }
        ],
        distance=distance,
        region=np.abs(distance) <= 0.002,
    )
    metrics, masks, _ = evaluate_targets(path, image, np.ones(image.shape, bool))
    edge = metrics["edge"]
    f, mtf = np.array(edge["frequency_cycles_m"]), np.array(edge["mtf"])
    keep = f < 2000
    np.testing.assert_allclose(mtf[keep], np.exp(-2 * (np.pi * sigma * f[keep]) ** 2), atol=0.015)
    assert edge["direction"] == direction
    np.testing.assert_array_equal(masks["target_edge"], np.abs(distance) <= 0.002)


def test_noise_excludes_incomplete_patch_and_preserves_psd_integral(tmp_path):
    image = np.arange(64, dtype=float).reshape(8, 8) / 100
    valid = np.ones(image.shape, bool)
    valid[0, 0] = False
    path = spec(
        tmp_path,
        [
            {
                "name": "noise",
                "kind": "noise",
                "patches": [[0, 4, 0, 4], [4, 8, 4, 8]],
                "pitch_x_m": 0.01,
                "pitch_y_m": 0.02,
            }
        ],
    )
    metrics, masks, _ = evaluate_targets(path, image, valid)
    noise = metrics["noise"]
    assert noise["accepted_patches"] == 1
    assert noise["rejected_patches"] == 1
    psd = np.array(noise["psd_od2_m2"])
    assert np.sum(psd) / (4 * 0.01 * 4 * 0.02) == pytest.approx(np.var(image[4:8, 4:8]))
    assert np.count_nonzero(masks["target_noise"]) == 16


def test_banding_geometry_and_step_respect_evaluation_mask(tmp_path):
    image = np.tile([0, 0, 0.5, 0.9, 1, 1, 1], (4, 1))
    image[[1, 3]] += 2
    valid = np.ones(image.shape, bool)
    valid[2] = False
    target = np.array([[0, 0], [0, 1], [0, 2], [0, 3]])
    path = spec(
        tmp_path,
        [
            {
                "name": "band",
                "kind": "banding",
                "facet_count": 2,
                "facet_phase": "phase.npy",
                "edge_positions_m": "edge.npy",
            },
            {
                "name": "grid",
                "kind": "geometry",
                "point_pixels": "pixels.npy",
                "actual_xy_m": "actual.npy",
                "target_xy_m": "target.npy",
                "row_y_m": "rows.npy",
            },
            {
                "name": "step",
                "kind": "step",
                "row": 0,
                "start_col": 0,
                "stop_col": 7,
                "edge_col": 2,
                "sample_period_s": 0.01,
            },
            {
                "name": "missing",
                "kind": "step",
                "row": 2,
                "start_col": 0,
                "stop_col": 7,
                "edge_col": 2,
                "sample_period_s": 0.01,
            },
        ],
        phase=np.array([0, 1, 0, 1]),
        edge=np.array([1, 3, 1, 3]),
        pixels=np.array([[0, 0], [1, 0], [2, 0], [3, 0]]),
        target=target,
        actual=target + np.column_stack((np.arange(4) * 0.1, np.zeros(4))),
        rows=np.arange(4),
    )
    metrics, _, _ = evaluate_targets(path, image, valid)
    assert metrics["band"]["edge_roughness_rms_m"] == 0
    assert metrics["band"]["periodic_components"][1]["amplitude_od"] == pytest.approx(1)
    assert metrics["grid"]["point_count"] == 3
    assert metrics["grid"]["scan_skew_dx_dy"] == pytest.approx(0.1)
    assert metrics["step"]["settling_s"] == pytest.approx(0.02)
    assert metrics["missing"]["status"] == "not_computed"


def test_duplicate_metric_names_and_non_boolean_regions_are_rejected(tmp_path):
    image = np.ones((4, 4))
    request = {
        "name": "noise",
        "kind": "noise",
        "region": "region.npy",
        "patches": [[0, 4, 0, 4]],
        "pitch_x_m": 1.0,
        "pitch_y_m": 1.0,
    }
    path = spec(tmp_path, [request], region=np.ones((4, 4)))
    with pytest.raises(ValueError, match="boolean"):
        evaluate_targets(path, image, np.ones((4, 4), bool))
    path = spec(tmp_path, [request, request], region=np.ones((4, 4), bool))
    with pytest.raises(ValueError, match="unique"):
        evaluate_targets(path, image, np.ones((4, 4), bool))


def test_mtf_includes_spot_bandwidth_and_sampling_aperture(tmp_path):
    y, x = np.mgrid[0:128, 0:256]
    distance = (x - 128 - y / 8) * 1e-4
    sigma, response_length, aperture = 1.5e-4, 2e-4, 1e-4
    nodes, weights = np.polynomial.legendre.leggauss(16)
    esf = (
        sum(
            w * exponnorm.cdf(distance + a * aperture / 2, response_length / sigma, scale=sigma)
            for a, w in zip(nodes, weights, strict=True)
        )
        / 2
    )
    density = -np.log10(0.2 + 0.7 * esf)
    path = spec(
        tmp_path,
        [
            {
                "name": "edge",
                "kind": "mtf",
                "direction": "scan",
                "distance_m": "distance.npy",
                "region": "region.npy",
                "bin_width_m": 1.25e-5,
                "working_density_od": 0.5,
                "scan_speed_m_s": 1.0,
            }
        ],
        distance=distance,
        region=np.abs(distance) <= 0.004,
    )
    metrics, _, _ = evaluate_targets(path, density, np.ones(density.shape, bool))
    f = np.array(metrics["edge"]["frequency_cycles_m"])
    mtf = np.array(metrics["edge"]["mtf"])
    theory = (
        np.exp(-2 * (np.pi * sigma * f) ** 2)
        / np.sqrt(1 + (2 * np.pi * response_length * f) ** 2)
        * np.abs(np.sinc(aperture * f))
    )
    np.testing.assert_allclose(mtf[f < 2000], theory[f < 2000], atol=0.015)


def test_wedge_does_not_bridge_an_unusable_middle_step(tmp_path):
    density = np.tile([0, 1.5, 2], (3, 1))
    path = spec(
        tmp_path,
        [
            {
                "name": "wedge",
                "kind": "wedge",
                "labels": "labels.npy",
                "levels_od": [0, 1, 2],
                "max_abs_bias_od": 0.1,
                "max_std_od": 0.01,
            }
        ],
        labels=np.tile(np.arange(3), (3, 1)),
    )
    metrics, _, _ = evaluate_targets(path, density, np.ones(density.shape, bool))
    assert metrics["wedge"]["usable_target_levels_od"] == [0, 2]
    assert metrics["wedge"]["usable_target_range_od"] is None


def test_missing_facet_group_does_not_invent_periodic_component(tmp_path):
    image = np.ones((4, 4))
    path = spec(
        tmp_path,
        [
            {
                "name": "band",
                "kind": "banding",
                "facet_count": 3,
                "facet_phase": "phase.npy",
                "edge_positions_m": "edge.npy",
            }
        ],
        phase=np.array([0, 1, 0, -1]),
        edge=np.ones(4),
    )
    metrics, masks, _ = evaluate_targets(path, image, np.ones(image.shape, bool))
    assert metrics["band"]["periodic_components"] is None
    assert "missing facet" in metrics["band"]["periodic_status"]
    assert metrics["band"]["unlocked_rows"] == 1
    assert not np.any(masks["target_band"][3])


def test_cli_unavailable_target_saves_report_and_returns_failure(tmp_path):
    image = np.ones((4, 4))
    np.savez(tmp_path / "density.npz", density=image, flags=np.zeros(image.shape, np.uint16))
    np.save(tmp_path / "reference.npy", image)
    path = spec(
        tmp_path,
        [
            {
                "name": "noise",
                "kind": "noise",
                "region": "region.npy",
                "patches": [[0, 4, 0, 4]],
                "pitch_x_m": 1,
                "pitch_y_m": 1,
            }
        ],
        region=np.zeros(image.shape, bool),
    )
    output = tmp_path / "metrics"
    assert (
        main(
            [
                "metrics",
                "--image",
                str(tmp_path / "density.npz"),
                "--truth",
                str(tmp_path / "reference.npy"),
                "--optical",
                str(tmp_path / "reference.npy"),
                "--targets",
                str(path),
                "--out",
                str(output),
            ]
        )
        != 0
    )
    report = json.loads((output / "run.json").read_text())
    assert report["target_metrics"]["noise"]["status"] == "not_computed"
    assert report["target_metrics"]["noise"]["accepted_patches"] == 0
    with np.load(output / "mask.npz") as masks:
        assert not np.any(masks["target_noise"])
