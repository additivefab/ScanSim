"""Conditional direct/surround/stray response and empirical sampled-noise identification."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import Field, model_validator
from scipy.stats import chi2

from zlscan.ident.conditional_linear import ConditionalLinear
from zlscan.ident.scope import Declaration, ScopeMetadata, import_scope_csv


class RadiometryExperiment(Declaration):
    name: str = Field(min_length=1)
    csv: str = Field(min_length=1)
    metadata: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    metadata_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    partition: Literal["fit", "validation"]
    channel: str = Field(min_length=1)
    window_s: tuple[float, float]
    light_on: bool
    incident_power_w: float = Field(ge=0)
    target_transmittance: float = Field(ge=0, le=1)
    surround_transmittance: float = Field(ge=0, le=1)
    mean_standard_uncertainty_si: float = Field(gt=0)
    uncertainty_source: str = Field(min_length=1)
    controls_source: str = Field(min_length=1)
    quality_source: str = Field(min_length=1)
    instrument_settings_ref: str = Field(min_length=1)

    @model_validator(mode="after")
    def ordered_window(self):
        if self.window_s[0] >= self.window_s[1]:
            raise ValueError("radiometry window must be ordered")
        return self


class RadiometryFitConfig(Declaration):
    format: Literal["zlscan.radiometry-fit-config/1"]
    response_unit: Literal["A", "V"]
    linear_response_ref: str = Field(min_length=1)
    instrument_settings_ref: str = Field(min_length=1)
    surround_definition_ref: str = Field(min_length=1)
    assumption_source: str = Field(min_length=1)
    confidence_level: float = Field(gt=0, lt=1)
    rank_rtol: float = Field(gt=0, lt=1)
    input_handling: Literal["conditional_on_declared_optical_controls"]
    noise_model: Literal["iid_gaussian_linear_variance_in_mean_response"]
    max_abs_lag1_correlation: float = Field(gt=0, lt=1)
    max_half_mean_shift_sigma: float = Field(gt=0)
    validation_max_abs_mean_residual_si: float | None = Field(default=None, ge=0)
    validation_max_variance_ratio: float | None = Field(default=None, gt=1)
    experiments: list[RadiometryExperiment] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def names_and_fit(self):
        if len({r.name for r in self.experiments}) != len(self.experiments):
            raise ValueError("duplicate radiometry experiment names")
        if not any(r.partition == "fit" for r in self.experiments):
            raise ValueError("radiometry needs at least one fit experiment")
        return self


def _design(row):
    q = float(row.light_on)
    return [
        1.0,
        q,
        q * row.incident_power_w * row.target_transmittance,
        q * row.incident_power_w * row.surround_transmittance,
    ]


def _prepare(config, directory):
    observations = []
    hashes = {}
    ids = {}
    signature = None
    for row in config.experiments:
        if row.instrument_settings_ref != config.instrument_settings_ref:
            raise ValueError("radiometry instrument settings differ")
        metadata_path = directory / row.metadata
        blob = metadata_path.read_bytes()
        if hashlib.sha256(blob).hexdigest() != row.metadata_sha256:
            raise ValueError("radiometry metadata SHA mismatch")
        meta = ScopeMetadata.model_validate_json(blob)
        if meta.source_kind not in {"synthetic", "declared_measurement"}:
            raise ValueError("radiometry source kind must be explicit")
        if len(meta.channels) != 1 or meta.channels[0].column != row.channel:
            raise ValueError("radiometry requires one explicitly selected linear channel")
        channel = meta.channels[0]
        if channel.coupling != "DC" or channel.probe_compensated is not True:
            raise ValueError("radiometry requires DC and declared compensated exported units")
        imported = import_scope_csv(directory / row.csv, meta)
        if imported["source_sha256"] != row.source_sha256:
            raise ValueError("radiometry source SHA mismatch")
        if imported["missing_metadata"] or imported["issues"]:
            raise ValueError("radiometry requires complete issue-free acquisition metadata")
        if imported["channels"][0]["unit_si"] != config.response_unit:
            raise ValueError("radiometry response unit does not match declaration")
        acquisition = (meta.session, meta.epoch, channel.acquisition_id)
        previous = hashes.get(row.source_sha256)
        if acquisition in ids and ids[acquisition] != row.source_sha256:
            raise ValueError("radiometry source or acquisition reuse")
        if previous is not None and (
            previous["partition"] != row.partition
            or previous["acquisition"] != acquisition
            or previous["metadata_sha256"] != row.metadata_sha256
        ):
            raise ValueError("radiometry source or acquisition reuse across partitions/identities")
        current = (
            meta.device,
            channel.measurement_point,
            meta.sample_interval_s,
            channel.bandwidth_limit_hz,
            channel.probe_ratio,
        )
        if signature is not None and current != signature:
            raise ValueError("radiometry instrument or observation bandwidth differs")
        signature = current
        t = np.asarray(imported["time_s"])
        y = np.asarray(imported["channels"][0]["values_si"])
        # Clock-declaration tolerance must never enlarge a selected signal window.
        tolerance = 4 * np.finfo(float).eps * max(1.0, abs(row.window_s[0]), abs(row.window_s[1]))
        if tolerance >= float(np.min(np.diff(t))) / 4:
            raise ValueError("radiometry times need relative export for precise window selection")
        if row.window_s[0] < t[0] - tolerance or row.window_s[1] > t[-1] + tolerance:
            raise ValueError("radiometry window extends beyond delivered samples")
        chosen = (t >= row.window_s[0] - tolerance) & (t <= row.window_s[1] + tolerance)
        indices = np.flatnonzero(chosen)
        y = y[chosen]
        t = t[chosen]
        if len(y) < 16:
            raise ValueError("radiometry window needs at least 16 delivered samples")
        span = [int(indices[0]), int(indices[-1])]
        if previous is not None and any(
            not (span[1] < first or last < span[0]) for first, last in previous["spans"]
        ):
            raise ValueError("radiometry overlapping delivered sample reuse")
        if previous is None:
            previous = dict(
                partition=row.partition,
                acquisition=acquisition,
                metadata_sha256=row.metadata_sha256,
                spans=[],
            )
            hashes[row.source_sha256] = previous
        previous["spans"].append(span)
        ids[acquisition] = row.source_sha256
        with np.errstate(over="ignore", invalid="ignore", under="ignore"):
            mean = float(np.mean(y))
            variance = float(np.var(y, ddof=1))
        n = len(y)
        mid = n // 2
        if not math.isfinite(mean) or not math.isfinite(variance):
            raise ValueError("radiometry mean or variance exceeds numerical range")
        corr = None
        shift = None
        passed = False
        if variance > 0:
            centered = (y - mean) / math.sqrt(variance)
            denom = math.sqrt(float(centered[:-1] @ centered[:-1])) * math.sqrt(
                float(centered[1:] @ centered[1:])
            )
            corr = float(centered[:-1] @ centered[1:] / denom) if denom > 0 else None
            shift = float(
                (np.mean(centered[:mid]) - np.mean(centered[mid:]))
                / math.sqrt(1 / mid + 1 / (n - mid))
            )
            passed = (
                corr is not None
                and abs(corr) <= config.max_abs_lag1_correlation
                and abs(shift) <= config.max_half_mean_shift_sigma
            )
        alpha = 1 - config.confidence_level
        interval = None
        if passed:
            interval = [
                float(variance / chi2.isf(alpha / 2, n - 1) * (n - 1)),
                float(variance / chi2.ppf(alpha / 2, n - 1) * (n - 1)),
            ]
        observations.append(
            dict(
                name=row.name,
                partition=row.partition,
                source_kind=meta.source_kind,
                declared=row.model_dump(mode="json"),
                source_sha256=row.source_sha256,
                metadata_sha256=row.metadata_sha256,
                metadata=meta.model_dump(mode="json"),
                sample_count=n,
                source_sample_range=span,
                time_extent_s=[float(t[0]), float(t[-1])],
                mean_si=mean,
                noise=dict(
                    variance_si2=variance,
                    variance_interval_si2=interval,
                    lag1_correlation=corr,
                    half_mean_shift_sigma=shift,
                    iid_diagnostic="passed_nominal_gate" if passed else "failed",
                    interval_scope=(
                        "chi-square conditional on iid Gaussian; "
                        "diagnostic does not prove assumption"
                    ),
                ),
            )
        )
    return observations


def identify_radiometry_files(config: RadiometryFitConfig, directory: Path) -> dict:
    config = RadiometryFitConfig.model_validate_json(config.model_dump_json())
    observations = _prepare(config, directory)
    result = dict(
        format="zlscan.radiometric-identification/1",
        origin="MODEL_ESTIMATE",
        response_unit=config.response_unit,
        config=config.model_dump(mode="json"),
        status="rejected_assumptions",
        observations=observations,
        mean_fit=None,
        noise_fit=None,
        glare_fraction=dict(value=None, standard_uncertainty=None, conditional_interval=None),
        validation=dict(status="not_executed", rows=[]),
        priors=[],
        parameter_upgrades=[],
        hard_acceptance_eligible=False,
        assumptions_independently_established=False,
        independence_established=False,
        synchronization="not_asserted_across_captures",
        mean_model="response = baseline + light_on*(lit_stray + P*direct_gain*T + P*glare_gain*S)",
        noise_model=(
            "variance = variance_at_reference + variance_slope*(predicted_mean-reference_mean)"
        ),
        interval_scope=(
            "mean: conditional known declared mean sigma; "
            "noise: plug-in Gaussian moment approximation"
        ),
        limitations=[
            "fixed optical controls and declared linear readout; no log-code averaging",
            "noise intervals assume iid Gaussian samples; lag/drift gates do not establish this",
            "noise covariance uses estimated variance errors and fitted mean as fixed inputs",
            "control, shared certificate, instrument and mean/noise joint errors excluded",
            "sampled variance is not spectral density or a time-grid convergence result",
            "direct/glare gains are combinations, not independent PMT or collection parameters",
            "source hashes and capture IDs do not prove authenticity or physical independence",
        ],
    )
    fitrows = [r for r in observations if r["partition"] == "fit"]
    if any(r["noise"]["iid_diagnostic"] == "failed" for r in fitrows):
        for r in observations:
            r["noise"]["variance_interval_si2"] = None
        json.dumps(result, allow_nan=False)
        return result
    byname = {r.name: r for r in config.experiments}
    unit = config.response_unit
    mean = ConditionalLinear(
        [_design(byname[r["name"]]) for r in fitrows],
        [r["mean_si"] for r in fitrows],
        [byname[r["name"]].mean_standard_uncertainty_si for r in fitrows],
        ["baseline", "lit_stray", "direct_gain", "glare_gain"],
        [unit, unit, unit + "/W", unit + "/W"],
        config.confidence_level,
        config.rank_rtol,
    )
    result["mean_fit"] = mean.report
    predicted = [mean.predict(_design(byname[r["name"]]), bounded=False)["value"] for r in fitrows]
    reference = min(predicted)
    noise = ConditionalLinear(
        [[1.0, v - reference] for v in predicted],
        [r["noise"]["variance_si2"] for r in fitrows],
        [r["noise"]["variance_si2"] * math.sqrt(2 / (r["sample_count"] - 1)) for r in fitrows],
        ["variance_at_reference", "variance_slope"],
        [unit + "^2", unit],
        config.confidence_level,
        config.rank_rtol,
    )
    result["noise_fit"] = dict(
        noise.report,
        reference_mean_si=reference,
        variance_support_mean_si=[min(predicted), max(predicted)],
        uncertainty_scope="nominal plug-in moment fit; not exact joint maximum likelihood",
    )
    params = mean.report["parameters"]
    direct, glare = (params[n]["value"] for n in ("direct_gain", "glare_gain"))
    if (
        direct is not None
        and glare is not None
        and direct >= 0
        and glare >= 0
        and direct + glare > 0
    ):
        total = direct + glare
        gradient = np.array([-glare / total**2, direct / total**2])
        variance = float(gradient @ mean.physical_cov[2:, 2:] @ gradient)
        if not math.isfinite(variance) or variance <= 0:
            raise ValueError("glare ratio uncertainty is numerically unresolved")
        se = math.sqrt(variance)
        fraction = glare / total
        result["glare_fraction"] = dict(
            value=fraction,
            standard_uncertainty=se,
            conditional_interval=[fraction - mean.z * se, fraction + mean.z * se],
            scope="delta-method ratio for declared center/surround protocol; not a measured PSF",
        )
    result["status"] = (
        "identified"
        if mean.report["rank"] == 4 and noise.report["rank"] == 2
        else "partially_identifiable"
    )
    if any(
        params[n]["value"] is not None and params[n]["value"] < 0
        for n in ("lit_stray", "direct_gain", "glare_gain")
    ):
        result["status"] = "nonphysical_mean_candidate"
    if any(p["value"] is not None and p["value"] < 0 for p in noise.report["parameters"].values()):
        result["status"] = "nonphysical_noise_candidate"
    if (
        noise.report["parameters"]["variance_at_reference"]["value"] is not None
        and noise.report["parameters"]["variance_at_reference"]["value"] <= 0
    ):
        result["status"] = "nonphysical_noise_candidate"
    validation = []
    mean_directions = []
    noise_directions = []
    for r in observations:
        declared = byname[r["name"]]
        design = _design(declared)
        mu = mean.predict(design)
        row = dict(
            name=r["name"],
            partition=r["partition"],
            mean_observed_si=r["mean_si"],
            mean_predicted_si=mu["value"],
            mean_residual_si=None,
            variance_observed_si2=r["noise"]["variance_si2"],
            variance_predicted_si2=None,
            variance_ratio=None,
            invalid_reason=mu["invalid_reason"],
        )
        if mu["value"] is not None:
            row["mean_residual_si"] = r["mean_si"] - mu["value"]
            nd = [1.0, mu["value"] - reference]
            var = noise.predict(nd)
            row["invalid_reason"] = var["invalid_reason"]
            if var["value"] is not None:
                if var["value"] <= 0:
                    row["invalid_reason"] = "nonpositive_variance_prediction"
                else:
                    row["variance_predicted_si2"] = var["value"]
                    row["variance_ratio"] = r["noise"]["variance_si2"] / var["value"]
            if declared.partition == "validation" and r["noise"]["iid_diagnostic"] == "failed":
                row["invalid_reason"] = "validation_noise_assumption_failed"
            if row["invalid_reason"] is None and declared.partition == "validation":
                mean_directions.append(design)
                noise_directions.append(nd)
        if declared.partition == "validation":
            validation.append(row)
        r["prediction"] = row
    coverage = dict(
        mean_rank_exercised=mean.exercised_rank(mean_directions),
        mean_fit_rank=mean.report["rank"],
        noise_rank_exercised=noise.exercised_rank(noise_directions),
        noise_fit_rank=noise.report["rank"],
    )
    mt, vt = config.validation_max_abs_mean_residual_si, config.validation_max_variance_ratio
    status = (
        "not_executed"
        if not validation
        else "rejected_assumptions"
        if any(
            r["noise"]["iid_diagnostic"] == "failed"
            for r in observations
            if r["partition"] == "validation"
        )
        else "unidentifiable"
        if any(r["invalid_reason"] for r in validation)
        else "incomplete_coverage"
        if coverage["mean_rank_exercised"] < coverage["mean_fit_rank"]
        or coverage["noise_rank_exercised"] < coverage["noise_fit_rank"]
        else "not_assessed"
        if mt is None or vt is None
        else "mismatch"
        if any(
            abs(r["mean_residual_si"]) > mt or not 1 / vt <= r["variance_ratio"] <= vt
            for r in validation
        )
        else "compatible"
    )
    result["validation"] = dict(
        status=status,
        rows=validation,
        conditional_direction_coverage=coverage,
        support_policy="normalized fit-design hull via HiGHS; noise mean inside fit range",
        threshold_scope="predeclared residual/variance-ratio limits; not a joint test region",
    )
    json.dumps(result, allow_nan=False)
    return result
