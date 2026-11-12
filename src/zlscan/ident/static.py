"""Conditional weighted linear identification with explicit held-out measurement IDs."""

from __future__ import annotations

import math

import numpy as np
from pydantic import Field, model_validator
from scipy.stats import chi2, norm

from zlscan.ident.scope import Declaration


class Feature(Declaration):
    name: str = Field(min_length=1)
    scale_si: float = Field(gt=0)


class ObservationPair(Declaration):
    inputs: dict[str, str]
    output: str = Field(min_length=1)


class StaticFitConfig(Declaration):
    version: int = Field(default=1, ge=1, le=1)
    features: list[Feature] = Field(min_length=1, max_length=16)
    intercept: bool
    confidence_level: float = Field(gt=0, lt=1)
    rank_rtol: float = Field(gt=0, lt=1)
    input_handling: str
    output_error_model: str
    assumption_source: str = Field(min_length=1)
    observations: list[ObservationPair] = Field(min_length=1, max_length=10000)
    validation_max_abs_residual_si: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def consistent_mapping(self) -> StaticFitConfig:
        names = [f.name for f in self.features]
        if len(set(names)) != len(names) or "intercept" in names:
            raise ValueError("feature names must be unique and cannot be intercept")
        if self.input_handling != "conditional_on_declared_values":
            raise ValueError("only explicit conditioning on declared input values is supported")
        if self.output_error_model != "independent_gaussian_known_standard_uncertainty":
            raise ValueError("known independent Gaussian output errors must be explicitly declared")
        if any(set(p.inputs) != set(names) for p in self.observations):
            raise ValueError("each observation must map every declared feature exactly once")
        return self


def identify_static(table: dict, config: StaticFitConfig) -> dict:
    """Use a freshly imported scalar table; never tune on validation or upgrade evidence.

    Confidence intervals condition on declared inputs and known independent output
    sigmas. They do not propagate input uncertainty, correlation or model discrepancy.
    Rank-deficient parameters are null; only supported combinations/predictions exist.
    """
    config = StaticFitConfig.model_validate(config.model_dump())
    result = {
        "format": "zlscan.static-identification/1",
        "origin": "MODEL_ESTIMATE",
        "data_source_kind": table.get("source_kind", "unknown"),
        "status": "rejected",
        "reasons": [],
        "config": config.model_dump(mode="json"),
        "parameters": {},
        "identifiable_combinations": [],
        "nullspace_scaled": [],
        "priors": [],
        "hard_acceptance_eligible": False,
        "parameter_upgrades": [],
        "independence_established": False,
        "interval_scope": "conditional marginal Gaussian intervals; input uncertainty excluded",
        "metadata_gaps": table.get("missing_metadata", []),
    }

    def reject(reason: str) -> dict:
        result["reasons"].append(reason)
        return result

    if table.get("format") != "zlscan.measurement-table/1":
        return reject("requires a freshly imported measurement table")
    if table["leakage"]:
        return reject("fit_validation_source_or_capture_reuse")
    by_id = {r["declared"]["measurement_id"]: r for r in table["rows"]}
    outputs = [p.output for p in config.observations]
    inputs = {mid for p in config.observations for mid in p.inputs.values()}
    if len(outputs) != len(set(outputs)) or inputs.intersection(outputs):
        return reject("output records cannot be reused or also used as inputs")
    names = [f.name for f in config.features]
    feature_units = None
    output_unit = None
    prepared = []
    ignored_input_uncertainty = []
    used = set()
    for pair in config.observations:
        ids = [pair.inputs[k] for k in names] + [pair.output]
        if any(mid not in by_id for mid in ids):
            return reject("unknown_measurement_id")
        rows = [by_id[mid] for mid in ids]
        partitions = {r["declared"]["partition"] for r in rows}
        if len(partitions) != 1 or not partitions <= {"fit", "validation"}:
            return reject("paired_records_must_share_fit_or_validation_partition")
        for r in rows:
            d = r["declared"]
            if not r["source_verified"] or any(
                d[k] is None for k in ["source_sha256", "session", "epoch", "acquisition_id"]
            ):
                return reject("verified_source_and_capture_identity_required")
            if r["value_si"] is None or r["issues"] or not math.isfinite(r["value_si"]):
                return reject("finite_issue_free_observation_required")
        units = [(r["declared"]["quantity"], r["unit_si"]) for r in rows[:-1]]
        yu = (rows[-1]["declared"]["quantity"], rows[-1]["unit_si"])
        if feature_units is None:
            feature_units, output_unit = units, yu
        if units != feature_units or yu != output_unit:
            return reject("inconsistent_observation_quantities_or_units")
        yrow = rows[-1]
        if (
            yrow["declared"]["uncertainty_kind"] != "standard"
            or not yrow["declared"]["uncertainty_source"]
            or yrow["uncertainty_si"] is None
            or yrow["uncertainty_si"] <= 0
            or not math.isfinite(yrow["uncertainty_si"])
        ):
            return reject("positive_declared_output_standard_uncertainty_required")
        used.update(ids)
        for name, r in zip(names, rows[:-1], strict=True):
            ignored_input_uncertainty.append(
                dict(
                    measurement_id=r["declared"]["measurement_id"],
                    feature=name,
                    uncertainty_si=r["uncertainty_si"],
                    uncertainty_kind=r["declared"]["uncertainty_kind"],
                )
            )
        prepared.append(
            dict(
                output_id=pair.output,
                partition=next(iter(partitions)),
                x=[r["value_si"] for r in rows[:-1]],
                y=yrow["value_si"],
                sigma=yrow["uncertainty_si"],
            )
        )
    fit = [r for r in prepared if r["partition"] == "fit"]
    validation = [r for r in prepared if r["partition"] == "validation"]
    if not fit:
        return reject("at_least_one_fit_observation_required")
    scales = np.asarray(([1.0] if config.intercept else []) + [f.scale_si for f in config.features])
    coefficient_names = (["intercept"] if config.intercept else []) + names

    def design(row: dict) -> np.ndarray:
        return np.asarray(([1.0] if config.intercept else []) + row["x"]) / scales

    d = np.asarray([design(r) for r in fit])
    sigma = np.asarray([r["sigma"] for r in fit])
    y = np.asarray([r["y"] for r in fit])
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        a, rhs = d / sigma[:, None], y / sigma
    if not np.isfinite(a).all() or not np.isfinite(rhs).all():
        return reject("nonfinite_scaled_design")
    # Full right basis is needed only when rows < columns; never allocate rows².
    u, s, vt = np.linalg.svd(a, full_matrices=a.shape[0] < a.shape[1])
    rank = int(np.sum(s > config.rank_rtol * s[0])) if s.size and s[0] > 0 else 0
    retained, nullspace = vt[:rank], vt[rank:]
    gamma = retained.T @ ((u[:, :rank].T @ rhs) / s[:rank])
    covariance = (
        (retained.T / s[:rank] ** 2) @ retained if rank else np.zeros((len(scales), len(scales)))
    )
    if not np.isfinite(gamma).all() or not np.isfinite(covariance).all():
        return reject("nonfinite_solution_or_covariance")
    z = float(norm.isf((1 - config.confidence_level) / 2))
    individually_identified = (
        np.linalg.norm(nullspace, axis=0) <= config.rank_rtol
        if nullspace.size
        else np.ones(len(scales), dtype=bool)
    )
    parameters = {}
    for i, name in enumerate(coefficient_names):
        value = float(gamma[i] / scales[i]) if individually_identified[i] else None
        se = math.sqrt(max(0.0, float(covariance[i, i]))) / scales[i] if value is not None else None
        parameters[name] = dict(
            value=value,
            standard_uncertainty=se,
            conditional_interval=[value - z * se, value + z * se] if value is not None else None,
            unit=output_unit[1]
            if name == "intercept"
            else f"{output_unit[1]}/{feature_units[names.index(name)][1]}",
        )
    physical_cov = covariance / np.outer(scales, scales)
    correlations = []
    masked_cov = []
    for i in range(len(scales)):
        masked_cov.append(
            [
                float(physical_cov[i, j])
                if individually_identified[i] and individually_identified[j]
                else None
                for j in range(len(scales))
            ]
        )
        correlations.append(
            [
                float(covariance[i, j] / math.sqrt(covariance[i, i] * covariance[j, j]))
                if individually_identified[i]
                and individually_identified[j]
                and covariance[i, i] > 0
                and covariance[j, j] > 0
                else None
                for j in range(len(scales))
            ]
        )
    combos = []
    for i, direction in enumerate(retained):
        value = float(direction @ gamma)
        se = float(1 / s[i])
        combos.append(
            dict(
                weights_scaled=direction.tolist(),
                value=value,
                standard_uncertainty=se,
                conditional_interval=[value - z * se, value + z * se],
            )
        )
    bounds = [(min(r["x"][j] for r in fit), max(r["x"][j] for r in fit)) for j in range(len(names))]

    def prediction(row: dict) -> dict:
        design_row = design(row)
        rec = dict(
            output_id=row["output_id"],
            observed=row["y"],
            prediction=None,
            residual=None,
            conditional_mean_interval=None,
            invalid_reason=None,
            outside_fit_feature_ranges=[
                names[j] for j, x in enumerate(row["x"]) if not bounds[j][0] <= x <= bounds[j][1]
            ],
        )
        if nullspace.size and np.linalg.norm(
            nullspace @ design_row
        ) > config.rank_rtol * np.linalg.norm(design_row):
            rec["invalid_reason"] = "prediction_not_identifiable"
            return rec
        value = float(design_row @ gamma)
        se = math.sqrt(max(0.0, float(design_row @ covariance @ design_row)))
        rec.update(
            prediction=value,
            residual=row["y"] - value,
            conditional_mean_interval=[value - z * se, value + z * se],
        )
        return rec

    fit_rows, val_rows = [prediction(r) for r in fit], [prediction(r) for r in validation]
    tolerance = config.validation_max_abs_residual_si
    if not val_rows:
        val_status = "not_executed"
    elif any(r["invalid_reason"] for r in val_rows):
        val_status = "unidentifiable"
    elif tolerance is None:
        val_status = "not_assessed"
    else:
        val_status = (
            "mismatch" if any(abs(r["residual"]) > tolerance for r in val_rows) else "compatible"
        )
    residual = np.asarray([r["y"] - design(r) @ gamma for r in fit])
    chi_square = float(np.sum((residual / sigma) ** 2))
    dof = len(fit) - rank
    result.update(
        status="identified"
        if rank == len(scales)
        else ("partially_identifiable" if rank else "unidentifiable"),
        output_unit_si=output_unit[1],
        output_quantity=output_unit[0],
        rank=rank,
        coefficient_order=coefficient_names,
        parameters=parameters,
        singular_values=s.tolist(),
        rank_threshold=config.rank_rtol * float(s[0]),
        condition_number=float(s[0] / s[-1]) if rank == len(scales) else None,
        identifiable_combinations=combos,
        nullspace_scaled=nullspace.tolist(),
        combination_coordinate="gamma = physical_coefficient * feature_scale_si; intercept scale 1",
        minimum_norm_scaled_solution_for_diagnostics=gamma.tolist(),
        conditional_covariance=masked_cov,
        conditional_correlation=correlations,
        input_uncertainty_ignored_conditionally=ignored_input_uncertainty,
        unused_measurement_ids=sorted(set(by_id) - used),
        fit_rows=fit_rows,
        fit_chi_square=chi_square,
        fit_degrees_of_freedom=dof,
        fit_chi_square_p_value=float(chi2.sf(chi_square, dof)) if dof > 0 else None,
        validation=dict(
            status=val_status,
            rows=val_rows,
            max_abs_residual_tolerance_si=tolerance,
            scope="nominal holdout residuals under declared model; independence unproven",
        ),
    )
    return result
