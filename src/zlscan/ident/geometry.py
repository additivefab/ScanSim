"""Conditional per-facet grid/slanted-target identification from hash-bound observations."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import Field, model_validator
from scipy.stats import chi2, norm

from zlscan.contracts.calibration import CaptureDescription
from zlscan.ident.scope import Declaration
from zlscan.io.facet_timing import FacetTiming, observed_phases
from zlscan.io.line_layout import line_layout
from zlscan.io.manifest import sha256_file
from zlscan.io.rawstream import FLAG_LINE_START, scan_blocks
from zlscan.recon.capture import _validate_capture


class GeometryLandmark(Declaration):
    name: str = Field(min_length=1)
    line: int = Field(ge=0)
    sample_column: float = Field(ge=0)
    target_x_m: float
    target_dx_dy: float
    standard_uncertainty_x_m: float = Field(gt=0)
    uncertainty_source: str = Field(min_length=1)


class GeometryObservations(Declaration):
    format: Literal["zlscan.geometry-observations/1"]
    source_kind: Literal["synthetic", "declared_measurement"]
    device: str = Field(min_length=1)
    measurement_point: str = Field(min_length=1)
    settings_source: str = Field(min_length=1)
    extraction_method: str = Field(min_length=1)
    target_certificate_ref: str = Field(min_length=1)
    acquisition_id: str = Field(min_length=1)
    raw_path: str = Field(min_length=1)
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    capture_path: str = Field(min_length=1)
    capture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    channel: int = Field(ge=0, le=15)
    timing: FacetTiming
    landmarks: list[GeometryLandmark] = Field(max_length=100000)

    @model_validator(mode="after")
    def unique_points(self):
        if self.timing.policy.facets > 64:
            raise ValueError("geometry software limit is 64 facets")
        if len({p.name for p in self.landmarks}) != len(self.landmarks):
            raise ValueError("duplicate geometric landmark names")
        if len({(p.line, p.sample_column) for p in self.landmarks}) != len(self.landmarks):
            raise ValueError("same observed landmark cannot be counted twice")
        return self


class GeometryInput(Declaration):
    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    partition: Literal["fit", "validation"]


class GeometryFitConfig(Declaration):
    format: Literal["zlscan.geometry-fit-config/1"]
    confidence_level: float = Field(gt=0, lt=1)
    rank_rtol: float = Field(gt=0, lt=1)
    assumption_source: str = Field(min_length=1)
    input_handling: Literal["conditional_on_declared_landmarks_and_slopes"]
    error_model: Literal["independent_gaussian_known_target_x_sigma"]
    validation_max_abs_residual_m: float | None = Field(default=None, ge=0)
    captures: list[GeometryInput] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def unique_inputs(self):
        if len({c.name for c in self.captures}) != len(self.captures):
            raise ValueError("duplicate geometry capture names")
        if not any(c.partition == "fit" for c in self.captures):
            raise ValueError("at least one fit capture required")
        return self


NAMES = ["x_offset_m", "x_linear_m", "x_cubic_m", "y_offset_m"]


def _design(point):
    u = point["u"]
    return np.array([1.0, u, u**3, -point["target_dx_dy"]])


def _fit(points, config):
    fit = [p for p in points if p["partition"] == "fit" and p["invalid_reason"] is None]
    x = np.array([_design(p) for p in fit]).reshape(-1, 4)
    sigmas = np.array([p["standard_uncertainty_x_m"] for p in fit])
    y = np.array([p["target_x_m"] for p in fit])
    scales = np.maximum(np.max(np.abs(x), axis=0), 1.0) if fit else np.ones(4)
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        a, rhs = x / scales / sigmas[:, None], y / sigmas
    if not np.isfinite(a).all() or not np.isfinite(rhs).all():
        raise ValueError("nonfinite weighted geometric design")
    try:
        u, s, vt = np.linalg.svd(a, full_matrices=len(fit) < 4)
    except np.linalg.LinAlgError as exc:
        raise ValueError("geometric SVD did not converge") from exc
    rank = int(np.sum(s > config.rank_rtol * s[0])) if len(s) and s[0] > 0 else 0
    retained, null = vt[:rank], vt[rank:]
    gamma = retained.T @ ((u[:, :rank].T @ rhs) / s[:rank])
    covariance = (retained.T / s[:rank] ** 2) @ retained if rank else np.zeros((4, 4))
    physical_covariance = covariance / np.outer(scales, scales)
    if not np.isfinite(gamma).all() or not np.isfinite(physical_covariance).all():
        raise ValueError("nonfinite geometric estimate or covariance")
    identified = np.linalg.norm(null, axis=0) <= config.rank_rtol if null.size else np.ones(4, bool)
    z = float(norm.isf((1 - config.confidence_level) / 2))
    parameters = {}
    for i, name in enumerate(NAMES):
        value = float(gamma[i] / scales[i]) if identified[i] else None
        se = math.sqrt(max(0.0, float(physical_covariance[i, i]))) if identified[i] else None
        if se == 0 and identified[i]:
            raise ValueError("geometric uncertainty below numerical range")
        parameters[name] = dict(
            value=value,
            standard_uncertainty=se,
            unit="m",
            conditional_interval=[value - z * se, value + z * se] if value is not None else None,
        )
    correlations, masked_cov = [], []
    for i in range(4):
        correlations.append(
            [
                float(covariance[i, j] / math.sqrt(covariance[i, i]) / math.sqrt(covariance[j, j]))
                if identified[i] and identified[j] and covariance[i, i] > 0 and covariance[j, j] > 0
                else None
                for j in range(4)
            ]
        )
        masked_cov.append(
            [
                float(physical_covariance[i, j]) if identified[i] and identified[j] else None
                for j in range(4)
            ]
        )
    support = [min(p["u"] for p in fit), max(p["u"] for p in fit)] if fit else None
    rows = []
    for point in points:
        row = dict(point, prediction_m=None, residual_m=None, conditional_mean_interval_m=None)
        d = _design(point) / scales
        if (
            row["invalid_reason"] is None
            and null.size
            and np.linalg.norm(null @ d) > config.rank_rtol * np.linalg.norm(d)
        ):
            row["invalid_reason"] = "prediction_not_identifiable"
        if row["invalid_reason"] is None and (
            support is None or not support[0] <= point["u"] <= support[1]
        ):
            row["invalid_reason"] = "outside_fit_support"
        if row["invalid_reason"] is None:
            value = float(d @ gamma)
            se = math.sqrt(max(0.0, float(d @ covariance @ d)))
            row.update(
                prediction_m=value,
                residual_m=point["target_x_m"] - value,
                conditional_mean_interval_m=[value - z * se, value + z * se],
            )
        rows.append(row)
    residual = rhs - a @ gamma
    score = float(residual @ residual)
    if not math.isfinite(score):
        raise ValueError("nonfinite geometric chi-square")
    mapping = "unidentifiable"
    if support and identified[1] and identified[2]:
        b, c = gamma[1:3] / scales[1:3]
        nearest = min(max(0.0, support[0]), support[1])
        derivative = b + 3 * c * np.array([support[0], support[1], nearest]) ** 2
        mapping = "monotonic_on_fit_support" if np.min(derivative) > 0 else "nonmonotonic"
    dof = len(fit) - rank
    return dict(
        rank=rank,
        coefficient_order=NAMES,
        parameters=parameters,
        status="identified"
        if rank == 4
        else "partially_identifiable"
        if rank
        else "unidentifiable",
        support_u=support,
        mapping_status=mapping,
        singular_values=s.tolist(),
        nullspace_scaled=null.tolist(),
        feature_scales=scales.tolist(),
        conditional_covariance=masked_cov,
        conditional_correlation=correlations,
        identifiable_combinations=[
            dict(
                weights_scaled=v.tolist(),
                value=float(v @ gamma),
                standard_uncertainty=float(1 / s[i]),
                conditional_interval=[float(v @ gamma - z / s[i]), float(v @ gamma + z / s[i])],
            )
            for i, v in enumerate(retained)
        ],
        combination_coordinate="gamma = coefficient_in_m * feature_scale; ordered [1,u,u^3,-slope]",
        fit_chi_square=score,
        fit_degrees_of_freedom=dof,
        fit_chi_square_tail_probability=float(chi2.sf(score, dof)) if dof > 0 else None,
        rows=rows,
    )


def identify_geometry_files(config: GeometryFitConfig, directory: Path) -> dict:
    """Validate actual source identities before fitting; validation points never enter the fit."""
    config = GeometryFitConfig.model_validate_json(config.model_dump_json())
    points, sources = [], []
    seen_hashes, seen_acquisitions = set(), set()
    identity, policy, window = None, None, None
    for declared in config.captures:
        path = directory / declared.path
        if sha256_file(path) != declared.sha256:
            raise ValueError("geometry annotation SHA mismatch")
        obs = GeometryObservations.model_validate_json(path.read_bytes())
        raw_path, capture_path = path.parent / obs.raw_path, path.parent / obs.capture_path
        if (
            sha256_file(raw_path) != obs.raw_sha256
            or sha256_file(capture_path) != obs.capture_sha256
        ):
            raise ValueError("geometry source SHA mismatch")
        desc = CaptureDescription.model_validate_json(capture_path.read_bytes())
        if desc.raw_sha256 != obs.raw_sha256 or desc.lines > 1000000:
            raise ValueError("geometry capture SHA or software line limit mismatch")
        raw = scan_blocks(raw_path, lambda *_: None)
        session = _validate_capture(raw, desc)
        if raw.errors:
            raise ValueError("damaged geometry raw source")
        if str(obs.channel) not in session.channel_roles:
            raise ValueError("geometry channel absent from raw session")
        if (session.origin == "SIMULATED") != (obs.source_kind == "synthetic"):
            raise ValueError("geometry source kind differs from raw origin")
        epochs = {b.epoch for b in raw.blocks}
        acquisition = (session.session_id, obs.acquisition_id)
        if obs.raw_sha256 in seen_hashes or acquisition in seen_acquisitions:
            raise ValueError("geometry source or acquisition reuse")
        seen_hashes.add(obs.raw_sha256)
        seen_acquisitions.add(acquisition)
        for epoch in epochs:
            binding = desc.binding_for_epoch(epoch).model_dump(mode="json", exclude={"epoch_range"})
            if (session.origin == "SIMULATED") != (binding["simulation_hash"] is not None):
                raise ValueError("geometry epoch origin mismatch")
            if identity is not None and binding != identity:
                raise ValueError("geometry instrument settings differ across sources")
            identity = binding
        current_policy = obs.timing.policy.model_dump(mode="json")
        if policy is not None and (policy != current_policy or window != session.windows):
            raise ValueError("geometry phase policy or acquisition window differs")
        policy, window = current_policy, session.windows
        channel_raw = replace(raw, blocks=[b for b in raw.blocks if obs.channel in b.channels])
        phases = observed_phases(obs.timing, channel_raw, obs.raw_sha256, desc.lines)
        anchor_epochs = {
            b.line_number: b.epoch for b in channel_raw.blocks if b.flags & FLAG_LINE_START
        }
        width = session.windows.get("window_length")
        if type(width) is not int or width <= 0:
            raise ValueError("geometry window length must be positive integer")
        layout = line_layout(raw.blocks, obs.channel, width)
        for p in obs.landmarks:
            if p.line >= desc.lines or p.sample_column > width - 1:
                raise ValueError("geometry landmark outside declared capture")
            landmark_epochs = set()
            for col in {math.floor(p.sample_column), math.ceil(p.sample_column)}:
                delivered = [
                    part
                    for part in layout.get(p.line, [])
                    if (
                        part.column_offset is not None
                        and part.column_offset <= col < part.column_offset + part.block.sample_count
                    )
                ]
                if not delivered:
                    raise ValueError("geometry landmark has no delivered source samples")
                landmark_epochs.update(part.block.epoch for part in delivered)
            phase = int(phases[p.line])
            if landmark_epochs != {anchor_epochs.get(p.line)}:
                phase = -1
            points.append(
                dict(
                    p.model_dump(mode="json"),
                    capture=declared.name,
                    partition=declared.partition,
                    phase=phase,
                    u=2 * (p.sample_column + 0.5) / width - 1,
                    invalid_reason=None if phase >= 0 else "facet_phase_unresolved",
                )
            )
        sources.append(
            dict(
                name=declared.name,
                partition=declared.partition,
                source_kind=obs.source_kind,
                raw_sha256=obs.raw_sha256,
                annotation_sha256=declared.sha256,
                declaration=obs.model_dump(mode="json", exclude={"landmarks", "timing"}),
            )
        )
    facets = {
        str(phase): _fit([p for p in points if p["phase"] == phase], config)
        for phase in range(policy["facets"])
    }
    statuses = [fit["status"] for fit in facets.values()]
    status = (
        "identified"
        if all(s == "identified" for s in statuses)
        else "partially_identifiable"
        if any(f["rank"] for f in facets.values())
        else "unidentifiable"
    )
    rows = [r for f in facets.values() for r in f["rows"] if r["partition"] == "validation"]
    rows.extend(
        dict(p, prediction_m=None, residual_m=None)
        for p in points
        if p["phase"] < 0 and p["partition"] == "validation"
    )
    if any(f["mapping_status"] == "nonmonotonic" for f in facets.values()):
        status = "invalid_geometry"
    coverage = {}
    for phase, fit in facets.items():
        scored = [r for r in rows if r["phase"] == int(phase) and r["residual_m"] is not None]
        design = np.array([_design(p) for p in scored]).reshape(-1, 4) / fit["feature_scales"]
        singular = np.linalg.svd(design, compute_uv=False)
        rank = int(np.sum(singular > config.rank_rtol * singular[0])) if len(singular) else 0
        coverage[phase] = dict(
            scored_rows=len(scored),
            conditional_design_rank=rank,
            fit_rank=fit["rank"],
            all_identified_directions_exercised=rank >= fit["rank"],
        )
    tolerance = config.validation_max_abs_residual_m
    validation = (
        "not_executed"
        if not any(c.partition == "validation" for c in config.captures)
        else "unidentifiable"
        if not rows or any(r["invalid_reason"] for r in rows)
        else "incomplete_coverage"
        if any(not c["all_identified_directions_exercised"] for c in coverage.values())
        else "not_assessed"
        if tolerance is None
        else "mismatch"
        if any(abs(r["residual_m"]) > tolerance for r in rows)
        else "compatible"
    )
    result = dict(
        format="zlscan.geometry-identification/1",
        origin="MODEL_ESTIMATE",
        status=status,
        config=config.model_dump(mode="json"),
        sources=sources,
        binding=identity,
        phase_policy=policy,
        facets=facets,
        validation=dict(status=validation, rows=rows, conditional_direction_coverage=coverage),
        excluded_unresolved_landmarks=[p for p in points if p["phase"] < 0],
        priors=[],
        parameter_upgrades=[],
        hard_acceptance_eligible=False,
        interval_scope=(
            "conditional marginal Gaussian; fixed landmarks/slopes, "
            "known independent target-x sigma"
        ),
        limitations=[
            "annotation extraction and certificate authenticity not independently established",
            "raw CRC/sample presence do not establish landmark accuracy or native model quality",
            "input localization, common target errors and model discrepancy not propagated",
            "distinct source and acquisition IDs do not prove physical independence",
            "no automatic calibration or measured-parameter upgrade",
        ],
        model="target_x_m = x_offset_m + x_linear_m*u + x_cubic_m*u^3 - target_dx_dy*y_offset_m",
    )
    json.dumps(result, allow_nan=False)
    return result
