"""Observable counter-tagged timestamps; weighted fits never invent missing syncs or phase."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import Field, model_validator
from scipy.stats import chi2, norm

from zlscan.contracts.events import Derivation, Event, Origin
from zlscan.contracts.verdict import Verdict
from zlscan.ident.scope import Declaration
from zlscan.io.manifest import sha256_file


class SyncEdge(Declaration):
    line_index: str = Field(pattern=r"^(0|[1-9][0-9]*)$")
    sync: Event
    anchor: Event | None = None


class SyncCapture(Declaration):
    format: Literal["zlscan.sync-observations/1"]
    source_kind: Literal["synthetic", "declared_measurement"]
    device: str = Field(min_length=1)
    measurement_point: str = Field(min_length=1)
    settings_source: str = Field(min_length=1)
    extraction_method: str = Field(min_length=1)
    raw_source: str = Field(min_length=1)
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    session_id: int = Field(ge=0, lt=2**64)
    epoch: int = Field(ge=0, lt=2**32)
    clock_domain: int
    counter_capability_ref: str = Field(min_length=1)
    phase_capability_ref: str | None = Field(default=None, min_length=1)
    edges: list[SyncEdge] = Field(min_length=3, max_length=100000)


class TimingInput(Declaration):
    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    partition: Literal["fit", "validation"]
    standard_uncertainty_ps: float = Field(gt=0)
    uncertainty_source: str = Field(min_length=1)


class TimingFitConfig(Declaration):
    format: Literal["zlscan.timing-fit-config/1"]
    facets: int = Field(ge=1, le=64)
    confidence_level: float = Field(gt=0, lt=1)
    rank_rtol: float = Field(gt=0, lt=1)
    assumption_source: str = Field(min_length=1)
    error_model: Literal["independent_gaussian_known_timestamp_sigma"]
    validation_max_abs_residual_ps: float | None = Field(default=None, ge=0)
    captures: list[TimingInput] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def unique_names_and_fit(self):
        if len({c.name for c in self.captures}) != len(self.captures):
            raise ValueError("duplicate timing capture names")
        if not any(c.partition == "fit" for c in self.captures):
            raise ValueError("at least one fit capture required")
        return self


def _counts(n, phase, facets):
    q, r = divmod(n, facets)
    value = [q] * facets
    for j in range(r):
        value[(phase + j) % facets] += 1
    return value


def _fit(rows, facets, config):
    if not rows:
        raise ValueError("at least one fit capture required")
    xs = []
    ys = []
    meta = []
    for row in rows:
        x = np.asarray(
            [_counts(i, row["phase"] or 0, facets) for i in row["relative"]], dtype=float
        )
        y = np.asarray(row["elapsed"], dtype=float)
        sigma = row["decl"].standard_uncertainty_ps
        # Project out a separate intercept in each capture. Original timestamp errors, not
        # their adjacent differences, have the declared independent covariance.
        xc = (x - x.mean(axis=0)) / sigma
        yc = (y - y.mean()) / sigma
        xs.append(xc)
        ys.append(yc)
        meta.append((row, x, y))
    x = np.vstack(xs)
    y = np.concatenate(ys)
    scales = np.maximum(np.max(np.abs(x), axis=0), 1.0)
    a = x / scales
    u, s, vt = np.linalg.svd(a, full_matrices=a.shape[0] < a.shape[1])
    rank = int(np.sum(s > config.rank_rtol * s[0])) if len(s) and s[0] > 0 else 0
    supported = vt[:rank]
    null = vt[rank:]
    gamma = supported.T @ ((u[:, :rank].T @ y) / s[:rank])
    cov = (supported.T / s[:rank] ** 2) @ supported if rank else np.zeros((facets, facets))
    beta = gamma / scales
    physical_cov = cov / np.outer(scales, scales)
    if not np.isfinite(beta).all() or not np.isfinite(physical_cov).all():
        raise ValueError("nonfinite timing fit or covariance")
    z = float(norm.isf((1 - config.confidence_level) / 2))

    def estimate(weights):
        d = np.asarray(weights, dtype=float) / scales
        if null.size and np.linalg.norm(null @ d) > config.rank_rtol * np.linalg.norm(d):
            return dict(value=None, standard_uncertainty=None, conditional_interval=None)
        value = float(d @ gamma)
        se = math.sqrt(max(0.0, float(d @ cov @ d)))
        return dict(
            value=value,
            standard_uncertainty=se,
            conditional_interval=[value - z * se, value + z * se],
        )

    names = ["period_ps"] if facets == 1 else [f"period_{i}_ps" for i in range(facets)]
    parameters = {name: estimate(np.eye(facets)[i]) for i, name in enumerate(names)}
    identified = [p["value"] is not None for p in parameters.values()]
    valid = all(p["value"] is None or p["value"] > 0 for p in parameters.values())
    residual = y - a @ gamma
    score = float(residual @ residual)
    dof = len(y) - len(rows) - rank
    cycle = estimate(np.ones(facets))
    cycle["unit"] = "ps"
    fit = dict(
        status="identified" if all(identified) and valid else "partially_identifiable",
        rank=rank + len(rows),
        period_rank=rank,
        nuisance_intercepts=len(rows),
        parameters=parameters,
        period_parameter_names=names,
        cycle_period=cycle,
        covariance_ps2=[
            [
                float(physical_cov[i, j]) if identified[i] and identified[j] else None
                for j in range(facets)
            ]
            for i in range(facets)
        ],
        correlation_ps=[
            [
                float(physical_cov[i, j] / math.sqrt(physical_cov[i, i] * physical_cov[j, j]))
                if identified[i]
                and identified[j]
                and physical_cov[i, i] > 0
                and physical_cov[j, j] > 0
                else None
                for j in range(facets)
            ]
            for i in range(facets)
        ],
        nullspace_scaled=null.tolist(),
        parameter_scales=scales.tolist(),
        identifiable_combinations=[
            dict(
                weights_scaled=v.tolist(),
                value=float(v @ gamma),
                standard_uncertainty=float(1 / ss),
            )
            for v, ss in zip(supported, s[:rank], strict=True)
        ],
        conditional_chi2=score,
        degrees_of_freedom=dof,
        conditional_chi2_p_value=float(chi2.sf(score, dof)) if dof > 0 else None,
        physical_periods_positive=valid if all(identified) else None,
        fit_curves=[
            dict(
                name=row["decl"].name,
                residual_ps=(y0 - (xx @ beta + (y0 - xx @ beta).mean())).tolist(),
            )
            for row, xx, y0 in meta
        ],
    )
    internal = dict(beta=beta, cov=physical_cov, null=null, scales=scales, valid=valid)
    return fit, internal


def identify_timing_files(config: TimingFitConfig, directory: Path) -> dict:
    config = TimingFitConfig.model_validate(config.model_dump())
    result = dict(
        format="zlscan.timing-identification/1",
        origin="MODEL_ESTIMATE",
        status="rejected",
        config=config.model_dump(mode="json"),
        reasons=[],
        captures=[],
        sources=[],
        hard_acceptance_eligible=False,
        parameter_upgrades=[],
        independence_established=False,
        synchronization="not_asserted_across_captures",
        priors=[],
        interval_scope=(
            "conditional known independent Gaussian timestamp errors; timebase systematics excluded"
        ),
        event_uncertainty_used_as_sigma=False,
        validation=dict(status="not_executed", rows=[]),
    )

    def reject(reason):
        result["reasons"].append(reason)
        return result

    rows = []
    hashes = set()
    identities = set()
    raw_hashes = set()
    unchanged = []
    total = 0
    shared = None
    phase_reference = None
    for decl in config.captures:
        path = directory / decl.path
        with path.open("rb") as stream:
            blob = stream.read((64 << 20) + 1)
        if len(blob) > 64 << 20:
            return reject("timing_attachment_exceeds_64_MiB")
        digest = hashlib.sha256(blob).hexdigest()
        if digest != decl.sha256:
            return reject("timing_source_sha256_mismatch")
        capture = SyncCapture.model_validate_json(blob)
        raw_path = path.parent / capture.raw_source
        raw_digest = sha256_file(raw_path)
        if raw_digest != capture.raw_sha256:
            return reject("raw_source_sha256_mismatch")
        identity = (capture.device, capture.session_id, capture.epoch, capture.clock_domain)
        if digest in hashes or raw_digest in raw_hashes or identity in identities:
            return reject("source_or_acquisition_reuse")
        hashes.add(digest)
        raw_hashes.add(raw_digest)
        identities.add(identity)
        key = (
            capture.device,
            capture.measurement_point,
            capture.source_kind,
            capture.counter_capability_ref,
            capture.phase_capability_ref,
        )
        if shared is None:
            shared = key
        # An absent phase reference does not change the counter measurement itself.
        if key[:4] != shared[:4]:
            return reject("inconsistent_counter_measurement_identity")
        unchanged.extend([(path, digest), (raw_path, raw_digest)])
        indices = []
        ticks = []
        anchors = []
        for edge in capture.edges:
            sync = edge.sync
            index = int(edge.line_index)
            if not 0 <= index < 2**64 or not -(2**63) <= sync.tick < 2**63:
                return reject("counter_or_tick_outside_integer_contract")
            if (
                sync.kind != "line_sync"
                or sync.recon_verdict() is not Verdict.PASS
                or sync.derivation is not Derivation.SENSOR
                or sync.capability_ref != capture.counter_capability_ref
                or sync.count != index
                or (sync.session_id, sync.epoch, sync.clock_domain)
                != (capture.session_id, capture.epoch, capture.clock_domain)
                or (sync.origin is Origin.SIMULATED) != (capture.source_kind == "synthetic")
            ):
                return reject("observable_counter_sensor_identity_required")
            if indices and (index <= indices[-1] or sync.tick <= ticks[-1]):
                return reject("duplicate_or_regressing_counter_or_tick")
            indices.append(index)
            ticks.append(sync.tick)
            if edge.anchor is not None:
                a = edge.anchor
                phase = a.payload.get("phase")
                if (
                    a.kind != "facet_index"
                    or a.recon_verdict() is not Verdict.PASS
                    or a.derivation is not Derivation.SENSOR
                    or not capture.phase_capability_ref
                    or a.capability_ref != capture.phase_capability_ref
                    or (a.tick, a.session_id, a.epoch, a.clock_domain, a.origin)
                    != (sync.tick, sync.session_id, sync.epoch, sync.clock_domain, sync.origin)
                    or type(phase) is not int
                    or not 0 <= phase < config.facets
                ):
                    return reject("observable_phase_anchor_required")
                anchors.append((index, phase))
        rel = [i - indices[0] for i in indices]
        elapsed = [t - ticks[0] for t in ticks]
        if rel[-1] > 2**53 or elapsed[-1] > 2**53:
            return reject("exact_difference_exceeds_float_fit_span")
        phase = None
        if anchors:
            phase = (anchors[0][1] - (anchors[0][0] - indices[0])) % config.facets
            if any((phase + i - indices[0]) % config.facets != p for i, p in anchors):
                return reject("conflicting_phase_anchors")
        if phase is not None:
            if phase_reference is not None and phase_reference != capture.phase_capability_ref:
                return reject("inconsistent_phase_reference")
            phase_reference = capture.phase_capability_ref
        total += len(indices)
        if total > 200000:
            return reject("timing_fit_exceeds_200000_edges")
        r = dict(decl=decl, relative=rel, elapsed=elapsed, phase=phase)
        rows.append(r)
        result["sources"].append(
            dict(
                name=decl.name,
                sha256=digest,
                raw_sha256=raw_digest,
                source_kind=capture.source_kind,
            )
        )
        intervals = []
        for i in range(1, len(indices)):
            dn, dt = indices[i] - indices[i - 1], ticks[i] - ticks[i - 1]
            intervals.append(
                dict(
                    start_line_index=str(indices[i - 1]),
                    end_line_index=str(indices[i]),
                    counter_delta=str(dn),
                    elapsed_ticks_ps=str(dt),
                    missing_counts=str(dn - 1),
                    span_average_period_ps=dt / dn,
                    span_average_line_rate_hz=1e12 * dn / dt,
                    conditional_period_standard_uncertainty_ps=math.sqrt(2)
                    * decl.standard_uncertainty_ps
                    / dn,
                    is_single_line_interval=dn == 1,
                    starting_phase=(phase + indices[i - 1] - indices[0]) % config.facets
                    if phase is not None
                    else None,
                )
            )
        single = [x["span_average_period_ps"] for x in intervals if x["is_single_line_interval"]]
        summary = dict(
            count=len(single),
            mean_ps=float(np.mean(single)) if single else None,
            descriptive_sample_sd_ps=float(np.std(single, ddof=1)) if len(single) > 1 else None,
            meaning=(
                "observed adjacent intervals; shared endpoints correlate errors; "
                "not physical rotation jitter"
            ),
        )
        result["captures"].append(
            dict(
                intervals=intervals,
                single_line_period_summary=summary,
                name=decl.name,
                partition=decl.partition,
                origin_tick_ps=str(ticks[0]),
                line_indices=[str(i) for i in indices],
                elapsed_ticks_ps=[str(t) for t in elapsed],
                phase_at_origin=phase,
                missing_line_counts=str(rel[-1] + 1 - len(indices)),
            )
        )
    fitting = [r for r in rows if r["decl"].partition == "fit"]
    global_fit, global_internal = _fit(fitting, 1, config)
    result["global_period_fit"] = global_fit
    anchored = [r for r in fitting if r["phase"] is not None]
    if config.facets == 1:
        facet_fit, internal = global_fit, global_internal
    elif anchored:
        facet_fit, internal = _fit(anchored, config.facets, config)
    else:
        facet_fit, internal = (
            dict(status="unidentifiable", reason="no_observable_phase_reference", parameters={}),
            None,
        )
    result["facet_period_fit"] = facet_fit
    result["excluded_unanchored_facet_fit"] = (
        [r["decl"].name for r in fitting if r["phase"] is None] if config.facets > 1 else []
    )
    result["status"] = (
        "identified"
        if facet_fit["status"] == "identified" and not result["excluded_unanchored_facet_fit"]
        else "partially_identifiable"
    )
    validation = []
    z = float(norm.isf((1 - config.confidence_level) / 2))
    for row in rows:
        if row["decl"].partition != "validation":
            continue
        predictions = []
        for n, observed in zip(row["relative"][1:], row["elapsed"][1:], strict=True):
            valid = (
                internal is not None
                and internal["valid"]
                and (config.facets == 1 or row["phase"] is not None)
            )
            x = np.asarray(_counts(n, row["phase"] or 0, config.facets), dtype=float)
            if valid:
                d = x / internal["scales"]
                null = internal["null"]
                valid = not null.size or np.linalg.norm(
                    null @ d
                ) <= config.rank_rtol * np.linalg.norm(d)
            prediction = residual = uncertainty = None
            if valid:
                prediction = float(x @ internal["beta"])
                residual = float(observed - prediction)
                uncertainty = math.sqrt(
                    float(x @ internal["cov"] @ x) + 2 * row["decl"].standard_uncertainty_ps ** 2
                )
            predictions.append(
                dict(
                    counter_delta=str(n),
                    observed_elapsed_ps=str(observed),
                    predicted_elapsed_ps=prediction,
                    residual_ps=residual,
                    nominal_prediction_error_standard_uncertainty_ps=uncertainty,
                    nominal_prediction_error_interval_ps=[-z * uncertainty, z * uncertainty]
                    if uncertainty is not None
                    else None,
                )
            )
        validation.append(
            dict(
                name=row["decl"].name,
                initial_anchor="first observed timestamp only; not scored",
                points=predictions,
            )
        )
    if validation:
        vals = [
            abs(p["residual_ps"])
            for r in validation
            for p in r["points"]
            if p["residual_ps"] is not None
        ]
        maximum = max(vals) if vals else None
        unknown = any(p["residual_ps"] is None for r in validation for p in r["points"])
        threshold = config.validation_max_abs_residual_ps
        status = (
            "unidentifiable"
            if unknown
            else "not_assessed"
            if threshold is None
            else "compatible"
            if maximum <= threshold
            else "mismatch"
        )
        result["validation"] = dict(
            status=status,
            rows=validation,
            max_abs_residual_ps=maximum,
            criterion_ps=threshold,
            covariance_note=(
                "residuals share an observed initial anchor and fitted parameters; "
                "do not treat points as independent"
            ),
        )
    if any(sha256_file(p) != digest for p, digest in unchanged):
        raise ValueError("timing sources changed during identification")
    return result
