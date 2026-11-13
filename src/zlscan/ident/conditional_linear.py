"""Small conditional weighted designs with estimability and convex-hull prediction support."""

from __future__ import annotations

import math

import numpy as np
from scipy.optimize import linprog
from scipy.stats import chi2, norm


class ConditionalLinear:
    def __init__(self, x, y, sigma, names, units, confidence, rtol):
        x, y, sigma = (np.asarray(v, dtype=float) for v in (x, y, sigma))
        if (
            x.ndim != 2
            or x.shape[0] != len(y)
            or y.ndim != 1
            or sigma.ndim != 1
            or y.shape != sigma.shape
            or len(names) != x.shape[1]
            or len(units) != len(names)
            or not len(names)
            or not 0 < confidence < 1
            or not 0 < rtol < 1
            or not len(y)
        ):
            raise ValueError("invalid conditional design dimensions")
        if (
            not np.isfinite(x).all()
            or not np.isfinite(y).all()
            or not np.isfinite(sigma).all()
            or np.any(sigma <= 0)
        ):
            raise ValueError("conditional fit requires finite design and positive known sigma")
        self.scales = np.max(np.abs(x), axis=0)
        self.scales[self.scales == 0] = 1.0
        self.design = x / self.scales
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            a, b = self.design / sigma[:, None], y / sigma
        if not np.isfinite(a).all() or not np.isfinite(b).all():
            raise ValueError("conditional weighted design exceeds numeric range")
        try:
            u, s, vt = np.linalg.svd(a, full_matrices=len(y) < x.shape[1])
        except np.linalg.LinAlgError as exc:
            raise ValueError("conditional SVD did not converge") from exc
        rank = int(np.sum(s > rtol * s[0])) if len(s) and s[0] > 0 else 0
        directions, self.null = vt[:rank], vt[rank:]
        self.gamma = directions.T @ ((u[:, :rank].T @ b) / s[:rank])
        with np.errstate(over="ignore", under="ignore", invalid="ignore", divide="ignore"):
            self.cov = (
                (directions.T / s[:rank] ** 2) @ directions
                if rank
                else np.zeros((len(names), len(names)))
            )
            self.physical_cov = self.cov / self.scales[:, None] / self.scales[None, :]
        if not np.isfinite(self.gamma).all() or not np.isfinite(self.physical_cov).all():
            raise ValueError("conditional solution or covariance exceeds numeric range")
        self.rtol, self.z = rtol, float(norm.isf((1 - confidence) / 2))
        self.support_tolerance = max(1e-10, min(1e-7, rtol))
        identified = (
            np.linalg.norm(self.null, axis=0) <= rtol
            if self.null.size
            else np.ones(len(names), bool)
        )
        params = {}
        for i, name in enumerate(names):
            value = float(self.gamma[i] / self.scales[i]) if identified[i] else None
            se = math.sqrt(max(0.0, float(self.physical_cov[i, i]))) if identified[i] else None
            if identified[i] and se == 0:
                raise ValueError("conditional uncertainty below numeric range")
            params[name] = dict(
                value=value,
                standard_uncertainty=se,
                unit=units[i],
                conditional_interval=[value - self.z * se, value + self.z * se]
                if value is not None
                else None,
            )
        covariance, correlation = [], []
        for i in range(len(names)):
            covariance.append(
                [
                    float(self.physical_cov[i, j]) if identified[i] and identified[j] else None
                    for j in range(len(names))
                ]
            )
            correlation.append(
                [
                    float(self.cov[i, j] / math.sqrt(self.cov[i, i]) / math.sqrt(self.cov[j, j]))
                    if identified[i] and identified[j]
                    else None
                    for j in range(len(names))
                ]
            )
        score = float(np.sum((a @ self.gamma - b) ** 2))
        dof = len(y) - rank
        if not math.isfinite(score):
            raise ValueError("nonfinite conditional chi-square")
        self.report = dict(
            rank=rank,
            coefficient_order=names,
            parameters=params,
            status="identified"
            if rank == len(names)
            else "partially_identifiable"
            if rank
            else "unidentifiable",
            singular_values=s.tolist(),
            feature_scales=self.scales.tolist(),
            nullspace_scaled=self.null.tolist(),
            conditional_covariance=covariance,
            conditional_correlation=correlation,
            identifiable_combinations=[
                dict(
                    weights_scaled=v.tolist(),
                    value=float(v @ self.gamma),
                    standard_uncertainty=float(1 / s[i]),
                    conditional_interval=[
                        float(v @ self.gamma - self.z / s[i]),
                        float(v @ self.gamma + self.z / s[i]),
                    ],
                )
                for i, v in enumerate(directions)
            ],
            combination_coordinate="gamma = physical coefficient * feature scale",
            fit_chi_square=score,
            fit_degrees_of_freedom=dof,
            fit_chi_square_tail_probability=float(chi2.sf(score, dof)) if dof > 0 else None,
            support_feasibility_tolerance=self.support_tolerance,
        )

    def predict(self, x, *, bounded=True):
        d = np.asarray(x, dtype=float) / self.scales
        if d.shape != self.scales.shape or not np.isfinite(d).all():
            raise ValueError("invalid conditional prediction design")
        if self.null.size and np.linalg.norm(self.null @ d) > self.rtol * np.linalg.norm(d):
            return dict(
                value=None, standard_uncertainty=None, invalid_reason="prediction_not_identifiable"
            )
        if bounded:
            support = linprog(
                np.zeros(len(self.design)),
                A_eq=np.vstack([np.ones(len(self.design)), self.design.T]),
                b_eq=np.r_[1.0, d],
                bounds=(0, None),
                method="highs",
                options=dict(
                    primal_feasibility_tolerance=self.support_tolerance,
                    dual_feasibility_tolerance=self.support_tolerance,
                ),
            )
            if support.status == 2:
                return dict(
                    value=None, standard_uncertainty=None, invalid_reason="outside_fit_design_hull"
                )
            if not support.success:
                raise ValueError("conditional convex-hull support check failed")
        se = math.sqrt(max(0.0, float(d @ self.cov @ d)))
        return dict(value=float(d @ self.gamma), standard_uncertainty=se, invalid_reason=None)

    def exercised_rank(self, designs):
        if not designs:
            return 0
        s = np.linalg.svd(np.asarray(designs) / self.scales, compute_uv=False)
        return int(np.sum(s > self.rtol * s[0])) if len(s) and s[0] > 0 else 0
