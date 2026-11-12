"""Identifiability, conditional uncertainty, and held-out use of scalar observations."""

import csv
import hashlib
import json

import numpy as np
import pytest

from zlscan.cli import main
from zlscan.ident.static import StaticFitConfig, identify_static
from zlscan.ident.table import COLUMNS, TableMetadata, import_measurement_table


def declaration(features=1, **changes):
    d = dict(
        features=[dict(name=f"x{i}", scale_si=1.0) for i in range(features)],
        intercept=True,
        confidence_level=0.95,
        rank_rtol=1e-10,
        input_handling="conditional_on_declared_values",
        output_error_model="independent_gaussian_known_standard_uncertainty",
        assumption_source="synthetic Gaussian test assumption",
        observations=[],
    )
    d.update(changes)
    return d


def fixture(tmp_path, xs=None, ys=None, val_x=None, val_y=None, features=1):
    xs = xs if xs is not None else [[0.0], [1.0], [2.0], [3.0]]
    ys = ys if ys is not None else [1.0, 3.0, 5.0, 7.0]
    val_x = val_x if val_x is not None else [[1.5]]
    val_y = val_y if val_y is not None else [4.0]
    rows, sources, observations = [], [], []
    for role, xx, yy in [("fit", xs, ys), ("validation", val_x, val_y)]:
        for i, (x, y) in enumerate(zip(xx, yy, strict=True)):
            key = f"{role}{i}"
            source = tmp_path / f"{key}.dat"
            source.write_bytes(key.encode())
            sha = hashlib.sha256(source.read_bytes()).hexdigest()
            sources.append(dict(path=source.name, sha256=sha))
            ids = {}
            for j, value in enumerate([*x, y]):
                r = dict.fromkeys(COLUMNS, "")
                mid = f"{key}_{j}"
                r.update(
                    measurement_id=mid,
                    reference_id=key,
                    partition=role,
                    quantity="voltage",
                    value=str(value),
                    unit="V",
                    uncertainty="0.1",
                    uncertainty_kind="standard",
                    uncertainty_source="synthetic sigma",
                    source_sha256=sha,
                    session="fixture",
                    epoch="0",
                    acquisition_id=key,
                )
                rows.append(r)
                if j < features:
                    ids[f"x{j}"] = mid
            observations.append(dict(inputs=ids, output=mid))
    path = tmp_path / "table.csv"
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    meta = TableMetadata(source_kind="synthetic", source_files=sources)
    report = import_measurement_table(path, meta, tmp_path)
    config = StaticFitConfig(**declaration(features, observations=observations))
    return path, meta, report, config


def test_known_affine_parameters_have_nonzero_conditional_intervals(tmp_path):
    _, _, table, config = fixture(tmp_path)
    r = identify_static(table, config)
    assert r["status"] == "identified"
    assert r["data_source_kind"] == "synthetic"
    assert r["parameters"]["intercept"]["value"] == pytest.approx(1.0)
    assert r["parameters"]["x0"]["value"] == pytest.approx(2.0)
    # Known independent output sigma=0.1: Var(slope)=0.01/sum((x-mean(x))^2).
    assert r["parameters"]["x0"]["standard_uncertainty"] == pytest.approx(np.sqrt(0.01 / 5))
    assert r["parameters"]["x0"]["conditional_interval"][1] > 2.0
    assert r["input_uncertainty_ignored_conditionally"]
    assert r["validation"]["status"] == "not_assessed"
    assert r["validation"]["rows"][0]["prediction"] == pytest.approx(4.0)
    assert not r["hard_acceptance_eligible"]
    assert r["parameter_upgrades"] == []


def test_validation_values_never_change_fitted_parameters_or_covariance(tmp_path):
    _, _, table, config = fixture(tmp_path)
    a = identify_static(table, config)
    for row in table["rows"]:
        if row["declared"]["partition"] == "validation":
            row["value_si"] += 1000
    b = identify_static(table, config)
    assert a["parameters"] == b["parameters"]
    assert a["conditional_covariance"] == b["conditional_covariance"]
    assert a["rank"] == b["rank"]


def test_collinear_features_report_combinations_not_invented_individual_gains(tmp_path):
    _, _, table, config = fixture(
        tmp_path,
        xs=[[0.0, 0.0], [1.0, 1.0], [2.0, 2.0], [3.0, 3.0]],
        val_x=[[1.0, 2.0]],
        features=2,
    )
    r = identify_static(table, config)
    assert r["status"] == "partially_identifiable"
    assert r["rank"] == 2
    assert r["parameters"]["intercept"]["value"] == pytest.approx(1.0)
    assert r["parameters"]["x0"]["value"] is None
    assert r["parameters"]["x1"]["value"] is None
    assert len(r["identifiable_combinations"]) == 2
    assert len(r["nullspace_scaled"]) == 1
    assert r["validation"]["rows"][0]["prediction"] is None
    assert r["validation"]["rows"][0]["invalid_reason"] == "prediction_not_identifiable"


@pytest.mark.parametrize("issue", ["leakage", "source", "sigma", "partition", "self_input"])
def test_invalid_evidence_or_pairing_is_refused(tmp_path, issue):
    _, _, table, config = fixture(tmp_path)
    if issue == "leakage":
        table["leakage"] = [{"kind": "source_sha256"}]
    elif issue == "source":
        table["rows"][0]["source_verified"] = False
    elif issue == "sigma":
        table["rows"][1]["declared"]["uncertainty_kind"] = "absolute_bound"
    elif issue == "partition":
        table["rows"][0]["declared"]["partition"] = "validation"
    else:
        config.observations[0].inputs["x0"] = config.observations[0].output
    r = identify_static(table, config)
    assert r["status"] == "rejected"
    assert r["parameters"] == {}
    assert r["reasons"]


def test_explicit_validation_threshold_reports_mismatch_without_refitting(tmp_path):
    _, _, table, config = fixture(tmp_path, val_y=[4.5])
    config.validation_max_abs_residual_si = 0.1
    r = identify_static(table, config)
    assert r["validation"]["status"] == "mismatch"
    assert r["parameters"]["x0"]["value"] == pytest.approx(2.0)


def test_cli_saves_parameters_residual_plot_and_failure_diagnostic(tmp_path):
    path, meta, _, config = fixture(tmp_path, val_y=[4.5])
    config.validation_max_abs_residual_si = 0.1
    mp = tmp_path / "meta.json"
    mp.write_text(meta.model_dump_json())
    cp = tmp_path / "fit.json"
    cp.write_text(config.model_dump_json())
    out = tmp_path / "out"
    assert (
        main(
            [
                "ident",
                "--table",
                str(path),
                "--metadata",
                str(mp),
                "--static-fit",
                str(cp),
                "--out",
                str(out),
            ]
        )
        != 0
    )
    r = json.loads((out / "run.json").read_text())
    assert r["static_identification"]["validation"]["status"] == "mismatch"
    assert "Conditional residuals" in (out / "static_residuals.svg").read_text()
    assert path.exists() and mp.exists()


def test_declared_numerical_feature_scale_does_not_change_physical_coefficients(tmp_path):
    _, _, table, config = fixture(tmp_path)
    a = identify_static(table, config)
    config.features[0].scale_si = 1000.0
    b = identify_static(table, config)
    assert b["parameters"]["x0"]["value"] == pytest.approx(a["parameters"]["x0"]["value"])
    assert b["parameters"]["x0"]["standard_uncertainty"] == pytest.approx(
        a["parameters"]["x0"]["standard_uncertainty"]
    )


def test_underdetermined_fit_reports_only_combination_but_supported_prediction(tmp_path):
    _, _, table, config = fixture(tmp_path, xs=[[1.0]], ys=[3.0], val_x=[[1.0]], val_y=[3.0])
    r = identify_static(table, config)
    assert r["status"] == "partially_identifiable"
    assert r["parameters"]["intercept"]["value"] is None
    assert r["parameters"]["x0"]["value"] is None
    assert r["validation"]["rows"][0]["prediction"] == pytest.approx(3.0)
    assert r["fit_degrees_of_freedom"] == 0
    assert r["fit_chi_square_p_value"] is None


def test_positive_holdout_tolerance_is_only_a_nominal_check(tmp_path):
    _, _, table, config = fixture(tmp_path, val_y=[4.01])
    config.validation_max_abs_residual_si = 0.1
    r = identify_static(table, config)
    assert r["validation"]["status"] == "compatible"
    assert not r["independence_established"]
    assert r["priors"] == []
    assert not r["hard_acceptance_eligible"]
