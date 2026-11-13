"""Conditional observed single-exponential response; no constituent-device inference."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import Field, model_validator
from scipy.optimize import brentq, minimize_scalar
from scipy.stats import chi2

from zlscan.ident.scope import Declaration, ScopeMetadata, import_scope_csv


class StepExperiment(Declaration):
    name: str = Field(min_length=1)
    csv: str = Field(min_length=1)
    metadata: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    channel: str = Field(min_length=1)
    partition: Literal["fit", "validation"]
    direction: Literal["rise", "fall"]
    step_time_s: float
    window_s: tuple[float, float]
    standard_uncertainty_si: float = Field(gt=0)
    uncertainty_source: str = Field(min_length=1)
    baseline_si: float | None = None
    amplitude_si: float | None = None
    levels_source: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def valid_window_and_levels(self):
        if self.window_s[0] >= self.window_s[1]:
            raise ValueError("step window must be ordered")
        if self.partition == "validation":
            if None in (self.baseline_si, self.amplitude_si, self.levels_source):
                raise ValueError("validation requires independently declared levels and source")
            if (self.amplitude_si > 0) != (self.direction == "rise") or self.amplitude_si == 0:
                raise ValueError("validation amplitude must match declared direction")
        elif any(x is not None for x in (self.baseline_si, self.amplitude_si, self.levels_source)):
            raise ValueError("fit levels are nuisance parameters, not validation declarations")
        return self


class DynamicFitConfig(Declaration):
    format: Literal["zlscan.dynamic-fit-config/1"]
    response_model: Literal["observed_single_exponential"]
    error_model: Literal["independent_gaussian_known_standard_uncertainty"]
    time_handling: Literal["conditional_on_declared_step_and_exported_times"]
    assumption_source: str = Field(min_length=1)
    tau_bounds_s: tuple[float, float]
    confidence_level: float = Field(gt=0, lt=1)
    rank_rtol: float = Field(gt=0, lt=1)
    validation_max_abs_residual_si: float | None = Field(default=None, ge=0)
    experiments: list[StepExperiment] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def valid_bounds_and_names(self):
        if not 0 < self.tau_bounds_s[0] < self.tau_bounds_s[1]:
            raise ValueError("positive ordered tau bounds required")
        names = [e.name for e in self.experiments]
        if len(names) != len(set(names)):
            raise ValueError("unique experiment names required")
        if not any(e.partition == "fit" for e in self.experiments):
            raise ValueError("at least one fit experiment required")
        return self


def _shape(t, step, tau):
    with np.errstate(over="ignore"):
        u = np.maximum(t - step, 0) / tau
    q = -np.expm1(-u)
    dq = -np.where(u < 745, u, 0) * np.exp(-np.minimum(u, 745))
    return q, dq


def _fit_direction(traces, config):
    lo, hi = np.log(config.tau_bounds_s)

    def profile(logtau, details=False):
        tau = float(np.exp(logtau))
        total = 0.0
        fits = []
        for row in traces:
            q, dq = _shape(row["t"], row["decl"].step_time_s, tau)
            design = np.column_stack([np.ones(len(q)), q])
            coef = np.linalg.lstsq(design, row["y"], rcond=config.rank_rtol)[0]
            residual = (row["y"] - design @ coef) / row["decl"].standard_uncertainty_si
            total += float(residual @ residual)
            fits.append((coef, design, dq, residual))
        return (total, fits) if details else total

    opt = minimize_scalar(profile, bounds=(lo, hi), method="bounded", options={"xatol": 1e-11})
    if not opt.success or not np.isfinite(opt.fun):
        raise ValueError("dynamic optimization did not converge to a finite profile")
    # Include both bounds in the search; an interior bounded-solver answer alone is insufficient.
    best = min((float(opt.x), float(lo), float(hi)), key=profile)
    score, fits = profile(best, True)
    tau = float(np.exp(best))
    threshold = float(chi2.ppf(config.confidence_level, 1))

    def delta(x):
        return profile(x) - score - threshold

    interval = None
    if lo < best < hi and delta(lo) > 0 and delta(hi) > 0:
        interval = [float(np.exp(brentq(delta, lo, best))), float(np.exp(brentq(delta, best, hi)))]
    count = sum(len(r["t"]) for r in traces)
    n = 2 * len(traces) + 1
    jac = np.zeros((count, n))
    names = []
    scales = []
    offset = 0
    curves = []
    direction = traces[0]["decl"].direction
    sign_ok = True
    for k, (row, (coef, design, dq, residual)) in enumerate(zip(traces, fits, strict=True)):
        sigma = row["decl"].standard_uncertainty_si
        end = offset + len(row["t"])
        # Dimensionless nuisance coordinates b/sigma, a/sigma and log(tau).
        jac[offset:end, 2 * k : 2 * k + 2] = design
        jac[offset:end, -1] = coef[1] * dq / sigma
        names.extend([row["decl"].name + ".baseline_si", row["decl"].name + ".amplitude_si"])
        scales.extend([sigma, sigma])
        sign_ok &= bool(coef[1] > 0 if direction == "rise" else coef[1] < 0)
        curves.append(
            dict(
                name=row["decl"].name,
                baseline_si=float(coef[0]),
                amplitude_si=float(coef[1]),
                predicted_si=(design @ coef).tolist(),
                residual_si=(residual * sigma).tolist(),
            )
        )
        offset = end
    names.append("tau_s")
    scales.append(tau)
    _, s, vh = np.linalg.svd(jac, full_matrices=False)
    rank = int(np.count_nonzero(s > config.rank_rtol * s[0])) if len(s) and s[0] > 0 else 0
    identifiable = interval is not None and rank == n and sign_ok
    covariance = correlation = None
    if identifiable:
        cov = (vh.T / s**2) @ vh
        cov = cov * np.outer(scales, scales)
        sd = np.sqrt(np.diag(cov))
        corr = cov / np.outer(sd, sd)
        if not np.isfinite(cov).all() or not np.isfinite(corr).all():
            raise ValueError("conditional dynamic covariance is nonfinite")
        covariance = cov.tolist()
        correlation = corr.tolist()
    return dict(
        status="identified" if identifiable else "unidentifiable",
        tau_s=tau if identifiable else None,
        tau_interval_s=interval if identifiable else None,
        profile_delta_chi2=threshold,
        profile_chi2_min=score,
        search_bounds_s=list(config.tau_bounds_s),
        rank=rank,
        parameters=n,
        degrees_of_freedom=count - n,
        conditional_chi2_p_value=float(chi2.sf(score, count - n)) if count > n else None,
        search_method="bounded scalar minimization in log tau; nominal connected profile interval",
        parameter_names=names,
        covariance=covariance,
        correlation=correlation,
        covariance_kind="local_conditional_known_sigma",
        reasons=[]
        if identifiable
        else [
            reason
            for reason, condition in (
                ("profile_interval_not_closed_within_declared_bounds", interval is None),
                ("rank_deficient", rank < n),
                ("amplitude_direction_mismatch", not sign_ok),
            )
            if condition
        ],
        fit_curves=curves,
        fit_curve_status="identified" if identifiable else "diagnostic_representative_only",
    )


def identify_dynamic_files(config: DynamicFitConfig, directory: Path) -> dict:
    """Fit only declared source partitions, with hashes and no evidence upgrades."""
    config = DynamicFitConfig.model_validate(config.model_dump())
    result = dict(
        format="zlscan.dynamic-identification/1",
        origin="MODEL_ESTIMATE",
        status="rejected",
        reasons=[],
        config=config.model_dump(mode="json"),
        directions={},
        traces=[],
        sources=[],
        hard_acceptance_eligible=False,
        parameter_upgrades=[],
        independence_established=False,
        synchronization="not_asserted_across_captures",
        priors=[],
        interval_scope=(
            "conditional one-parameter profile likelihood, nominal chi-square cutoff; "
            "known independent Gaussian output errors; "
            "step/time/input/instrument uncertainty excluded"
        ),
        validation=dict(status="not_executed", rows=[]),
    )

    def reject(reason):
        result["reasons"].append(reason)
        return result

    traces = []
    sources = set()
    identities = set()
    unit = None
    total = 0
    unchanged = []
    for decl in config.experiments:
        path = directory / decl.csv
        meta_path = directory / decl.metadata
        blob = meta_path.read_bytes()
        meta = ScopeMetadata.model_validate_json(blob)
        capture = import_scope_csv(path, meta)
        digest = capture["source_sha256"]
        result["sources"].append(
            dict(
                name=decl.name,
                source_sha256=digest,
                metadata_sha256=hashlib.sha256(blob).hexdigest(),
                source_kind=capture["source_kind"],
            )
        )
        if digest != decl.source_sha256:
            return reject("source_sha256_mismatch")
        if capture["missing_metadata"] or capture["issues"] or meta.source_kind == "unknown":
            return reject("complete_issue_free_declared_metadata_required")
        selected = next((c for c in capture["channels"] if c["column"] == decl.channel), None)
        channel = next((c for c in meta.channels if c.column == decl.channel), None)
        if channel is None or channel.coupling != "DC" or not channel.probe_compensated:
            return reject("selected_compensated_DC_channel_required")
        identity = (meta.device, meta.session, meta.epoch, channel.acquisition_id)
        if digest in sources or identity in identities:
            return reject("source_or_acquisition_reuse")
        sources.add(digest)
        identities.add(identity)
        if unit is None:
            unit = selected["unit_si"]
        if unit != selected["unit_si"]:
            return reject("inconsistent_output_units")
        t = np.asarray(capture["time_s"])
        y = np.asarray(selected["values_si"])
        a, b = decl.window_s
        mask = (t >= a) & (t <= b)
        if a < t[0] or b > t[-1] or np.count_nonzero(mask) < 6:
            return reject("window_outside_capture_or_fewer_than_six_samples")
        t, y = t[mask], y[mask]
        total += len(t)
        if total > 200000:
            return reject("dynamic_fit_exceeds_200000_selected_samples")
        if not np.any(t > decl.step_time_s):
            return reject("no_post_step_samples")
        traces.append(dict(decl=decl, t=t, y=y))
        unchanged.extend([(path, digest), (meta_path, hashlib.sha256(blob).hexdigest())])
    result["unit_si"] = unit
    for direction in ("rise", "fall"):
        fit = [
            r for r in traces if r["decl"].direction == direction and r["decl"].partition == "fit"
        ]
        if fit:
            result["directions"][direction] = _fit_direction(fit, config)
    if any(r["decl"].direction not in result["directions"] for r in traces):
        return reject("validation_direction_has_no_fit")
    result["status"] = (
        "identified"
        if all(r["status"] == "identified" for r in result["directions"].values())
        else "unidentifiable"
    )
    held = []
    for row in traces:
        d = row["decl"]
        fit = result["directions"][d.direction]
        if d.partition == "fit":
            curve = next(c for c in fit["fit_curves"] if c["name"] == d.name)
            predicted = curve["predicted_si"]
            residual = curve["residual_si"]
        elif fit["tau_s"] is None:
            predicted = [None] * len(row["t"])
            residual = [None] * len(row["t"])
        else:
            q, _ = _shape(row["t"], d.step_time_s, fit["tau_s"])
            pred = d.baseline_si + d.amplitude_si * q
            predicted = pred.tolist()
            residual = (row["y"] - pred).tolist()
        r = dict(
            name=d.name,
            direction=d.direction,
            partition=d.partition,
            time_s=row["t"].tolist(),
            observed_si=row["y"].tolist(),
            predicted_si=predicted,
            residual_si=residual,
        )
        result["traces"].append(r)
        if d.partition == "validation":
            held.append(r)
    if held:
        values = [abs(x) for r in held for x in r["residual_si"] if x is not None]
        maximum = max(values) if values else None
        status = (
            "unidentifiable"
            if any(None in r["predicted_si"] for r in held)
            else "not_assessed"
            if config.validation_max_abs_residual_si is None
            else "compatible"
            if maximum <= config.validation_max_abs_residual_si
            else "mismatch"
        )
        result["validation"] = dict(
            status=status,
            rows=[r["name"] for r in held],
            max_abs_residual_si=maximum,
            criterion_si=config.validation_max_abs_residual_si,
        )
    if any(hashlib.sha256(p.read_bytes()).hexdigest() != digest for p, digest in unchanged):
        raise ValueError("dynamic source changed during identification")
    return result
