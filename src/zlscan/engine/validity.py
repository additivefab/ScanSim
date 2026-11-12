"""Model applicability is distinct from successful execution and from device validation."""

from __future__ import annotations

import math

from zlscan.engine.chain import ChainConfig


def physical_applicability(cfg: ChainConfig) -> dict:
    noisy_log = cfg.detector.shot_noise and cfg.afe.kind == "log"
    response = cfg.detector.pole_hz is not None
    dt_over_tau = 2 * math.pi * cfg.detector.pole_hz / (cfg.fs_hz * cfg.os) if response else None
    limits = [
        "PMT anode limit is a warning only, not saturation or recovery physics",
        "polygon scanner, not a closed-loop galvanometer",
        "collection model does not establish local coating-defect response",
        "assumed parameters do not establish hardware capability",
    ]
    if noisy_log and not response:
        limits.insert(
            0,
            "charge-bin current enters the log without a fixed physical input response; "
            "statistics depend on the numerical grid",
        )
    elif response:
        limits.insert(
            0,
            "one assumed input pole is a research candidate, "
            "not a measured PMT/electrical response",
        )
    if cfg.scan.legacy_clamped_coordinates:
        limits.insert(
            0, "legacy coordinate clamp reproduces an obsolete V5 bug; not a physical trajectory"
        )
    if any(scene.legacy_nearest_boundary for scene in cfg.scenes.values()):
        limits.insert(
            0, "legacy scene extends the edge beyond the film; not a transparent off-film model"
        )
    return {
        "status": "limited_research_model",
        "high_density_capability": "not_validated",
        "stochastic_log_status": (
            "grid_dependent_exploration_only"
            if noisy_log and not response
            else "assumed_fixed_response_requires_statistical_convergence"
            if noisy_log
            else "not_applicable"
        ),
        "input_response_hz": cfg.detector.pole_hz,
        "trajectory_status": (
            "legacy_clamped_coordinates"
            if cfg.scan.legacy_clamped_coordinates
            else "unclamped_f_theta_with_assumed_extrapolation"
        ),
        "input_response_evidence": "assumed" if response else None,
        "dt_over_response_tau": dt_over_tau,
        "finite_grid_to_continuous_current_variance_ratio": (
            2 * math.tanh(dt_over_tau / 2) / dt_over_tau if response else None
        ),
        "limitations": limits,
    }
