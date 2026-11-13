"""Conditional observed exponential identification, never constituent hardware parameters."""

import hashlib
import json

import numpy as np
import pytest

from zlscan.ident.dynamic import DynamicFitConfig, identify_dynamic_files


def fixture(root):
    root.mkdir(exist_ok=True)
    cases = []
    for i, (direction, partition, tau, amplitude) in enumerate(
        [
            ("rise", "fit", 0.002, 1.5),
            ("fall", "fit", 0.005, -1.2),
            ("rise", "validation", 0.002, 1.5),
            ("fall", "validation", 0.005, -1.2),
        ]
    ):
        t = np.linspace(-0.003, 0.04, 173)
        y = 0.3 + amplitude * (-np.expm1(-np.maximum(t, 0) / tau))
        # Separate acquisitions have different time exports (and therefore source digests).
        t = t + i * 0.1
        p = root / f"capture-{i}.csv"
        np.savetxt(p, np.column_stack([t, y]), delimiter=",", header="t,y", comments="")
        meta = dict(
            time_column="t",
            time_unit="s",
            source_kind="synthetic",
            device="synthetic-test",
            sample_interval_s=float(t[1] - t[0]),
            time_tolerance_s=1e-12,
            trigger_source="declared step",
            trigger_threshold_v=0.5,
            time_origin="explicit time export",
            session=f"session-{i}",
            epoch=0,
            simultaneous=True,
            channels=[
                dict(
                    column="y",
                    unit="V",
                    measurement_point="observed chain",
                    probe_ratio=1.0,
                    coupling="DC",
                    bandwidth_limit_hz=0.0,
                    probe_compensated=True,
                    acquisition_id=f"acq-{i}",
                )
            ],
        )
        m = root / f"meta-{i}.json"
        m.write_text(json.dumps(meta))
        cases.append(
            dict(
                name=f"case-{i}",
                csv=p.name,
                metadata=m.name,
                source_sha256=hashlib.sha256(p.read_bytes()).hexdigest(),
                channel="y",
                partition=partition,
                direction=direction,
                step_time_s=i * 0.1,
                window_s=[float(t[0]), float(t[-1])],
                standard_uncertainty_si=0.001,
                uncertainty_source="synthetic independent Gaussian protocol",
                baseline_si=0.3 if partition == "validation" else None,
                amplitude_si=amplitude if partition == "validation" else None,
                levels_source="independent protocol" if partition == "validation" else None,
            )
        )
    return dict(
        format="zlscan.dynamic-fit-config/1",
        response_model="observed_single_exponential",
        error_model="independent_gaussian_known_standard_uncertainty",
        time_handling="conditional_on_declared_step_and_exported_times",
        assumption_source="synthetic analytic fixture",
        tau_bounds_s=[0.0001, 0.05],
        confidence_level=0.95,
        rank_rtol=1e-10,
        validation_max_abs_residual_si=0.005,
        experiments=cases,
    )


def run(root, c):
    return identify_dynamic_files(DynamicFitConfig.model_validate_json(json.dumps(c)), root)


def test_recovers_separate_rise_fall_and_profile_intervals(tmp_path):
    c = fixture(tmp_path)
    r = run(tmp_path, c)
    assert r["status"] == "identified"
    for name, expected in [("rise", 0.002), ("fall", 0.005)]:
        fit = r["directions"][name]
        assert fit["tau_s"] == pytest.approx(expected, rel=1e-5)
        assert fit["tau_interval_s"][0] < expected < fit["tau_interval_s"][1]
        assert fit["covariance_kind"] == "local_conditional_known_sigma"
        assert fit["correlation"] is not None
    assert r["validation"]["status"] == "compatible"
    assert not r["hard_acceptance_eligible"] and not r["parameter_upgrades"]
    assert r["origin"] == "MODEL_ESTIMATE" and not r["independence_established"]


def test_holdout_changes_neither_tau_nor_profile(tmp_path):
    c = fixture(tmp_path)
    a = run(tmp_path, c)
    case = c["experiments"][2]
    p = tmp_path / case["csv"]
    data = np.loadtxt(p, delimiter=",", skiprows=1)
    data[:, 1] += 0.2
    np.savetxt(p, data, delimiter=",", header="t,y", comments="")
    case["source_sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
    b = run(tmp_path, c)
    assert a["directions"] == b["directions"] and b["validation"]["status"] == "mismatch"


@pytest.mark.parametrize("mode", ["digest", "identity", "missing", "ac", "sign", "flat", "bound"])
def test_invalid_or_unidentifiable_not_silently_certified(tmp_path, mode):
    c = fixture(tmp_path)
    case = c["experiments"][0]
    p = tmp_path / case["csv"]
    if mode == "digest":
        case["source_sha256"] = "0" * 64
    elif mode == "identity":
        m = tmp_path / c["experiments"][2]["metadata"]
        d = json.loads(m.read_text())
        d["session"] = "session-0"
        d["channels"][0]["acquisition_id"] = "acq-0"
        m.write_text(json.dumps(d))
    elif mode in ("missing", "ac"):
        m = tmp_path / case["metadata"]
        d = json.loads(m.read_text())
        d["channels"][0]["coupling"] = "AC" if mode == "ac" else None
        m.write_text(json.dumps(d))
    elif mode in ("flat", "sign"):
        d = np.loadtxt(p, delimiter=",", skiprows=1)
        d[:, 1] = 0.3 if mode == "flat" else -d[:, 1]
        np.savetxt(p, d, delimiter=",", header="t,y", comments="")
        case["source_sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
    else:
        c["tau_bounds_s"] = [0.0001, 0.001]
    r = run(tmp_path, c)
    assert r["status"] != "identified"
    assert not r["parameter_upgrades"]
    if mode in ("flat", "bound"):
        assert r["directions"]["rise"]["tau_s"] is None


def test_no_validation_or_threshold_never_passes(tmp_path):
    c = fixture(tmp_path)
    c["validation_max_abs_residual_si"] = None
    assert run(tmp_path, c)["validation"]["status"] == "not_assessed"
    c["experiments"] = c["experiments"][:2]
    assert run(tmp_path, c)["validation"]["status"] == "not_executed"


def test_validation_levels_and_explicit_error_assumptions_required(tmp_path):
    c = fixture(tmp_path)
    c["experiments"][2]["levels_source"] = None
    with pytest.raises(ValueError):
        DynamicFitConfig.model_validate_json(json.dumps(c))
    c = fixture(tmp_path)
    c["error_model"] = "unknown"
    with pytest.raises(ValueError):
        DynamicFitConfig.model_validate_json(json.dumps(c))


def test_cli_residual_artifacts_and_mismatch_exit(tmp_path):
    from zlscan.cli import main

    c = fixture(tmp_path)
    path = tmp_path / "dynamic.json"
    path.write_text(json.dumps(c))
    assert main(["ident", "--dynamic-fit", str(path), "--out", str(tmp_path / "out")]) == 0
    result = json.loads((tmp_path / "out/run.json").read_text())
    assert result["validation"]["status"] == "compatible"
    assert (tmp_path / "out/dynamic_residuals.svg").stat().st_size > 1000
    assert (tmp_path / "out/dynamic_residuals.png").read_bytes().startswith(b"\x89PNG")
    case = c["experiments"][2]
    p = tmp_path / case["csv"]
    data = np.loadtxt(p, delimiter=",", skiprows=1)
    data[:, 1] += 0.3
    np.savetxt(p, data, delimiter=",", header="t,y", comments="")
    case["source_sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
    path.write_text(json.dumps(c))
    assert main(["ident", "--dynamic-fit", str(path), "--out", str(tmp_path / "bad")]) == 1
    assert json.loads((tmp_path / "bad/run.json").read_text())["validation"]["status"] == "mismatch"
    with pytest.raises(SystemExit):
        main(["ident", "--dynamic-fit", str(path), "--metadata", "x", "--out", "x"])


def test_intervals_use_declared_sigma_not_perfect_fit_residual_scale(tmp_path):
    c = fixture(tmp_path)
    a = run(tmp_path, c)
    for e in c["experiments"]:
        e["standard_uncertainty_si"] *= 2
    b = run(tmp_path, c)
    for direction in ("rise", "fall"):
        x, y = a["directions"][direction], b["directions"][direction]
        assert y["tau_s"] == pytest.approx(x["tau_s"], rel=1e-8)
        assert y["tau_interval_s"][1] - y["tau_interval_s"][0] > 1.9 * (
            x["tau_interval_s"][1] - x["tau_interval_s"][0]
        )


def test_multiple_offsets_per_direction_are_profiled_without_sharing_levels(tmp_path):
    c = fixture(tmp_path)
    extra = dict(c["experiments"][0])
    extra.update(name="extra", csv="extra.csv", metadata="extra.json")
    p = tmp_path / extra["csv"]
    d = np.loadtxt(tmp_path / "capture-0.csv", delimiter=",", skiprows=1)
    d[:, 1] = 0.7 + 2 * (d[:, 1] - 0.3)
    np.savetxt(p, d, delimiter=",", header="t,y", comments="")
    extra["source_sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
    m = json.loads((tmp_path / "meta-0.json").read_text())
    m["session"] = "extra"
    m["channels"][0]["acquisition_id"] = "extra"
    (tmp_path / extra["metadata"]).write_text(json.dumps(m))
    c["experiments"].append(extra)
    r = run(tmp_path, c)
    fit = r["directions"]["rise"]
    assert fit["tau_s"] == pytest.approx(0.002, rel=1e-5)
    assert fit["parameters"] == 5 and len(fit["correlation"]) == 5
    assert fit["fit_curves"][1]["baseline_si"] == pytest.approx(0.7, abs=1e-7)
    assert fit["fit_curves"][1]["amplitude_si"] == pytest.approx(3.0, rel=1e-6)
