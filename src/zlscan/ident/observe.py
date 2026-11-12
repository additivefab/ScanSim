"""Attach an explicitly aligned one-channel model estimate to a scope import."""

from __future__ import annotations

import numpy as np

from zlscan.observation.scope import ScopeObserverConfig, observe_voltage


def attach_scope_observer(
    capture: dict,
    model_time_s: np.ndarray,
    model_voltage_v: np.ndarray,
    config: ScopeObserverConfig,
    channel: str,
) -> dict:
    selected = next((c for c in capture["channels"] if c["column"] == channel), None)
    if selected is None or selected["unit_si"] != "V":
        raise ValueError("observation comparison requires one selected voltage channel")
    declaration = next(c for c in capture["metadata"]["channels"] if c["column"] == channel)
    for field in ["probe_ratio", "probe_compensated", "coupling", "bandwidth_limit_hz"]:
        if declaration.get(field) is not None and declaration[field] != getattr(config, field):
            raise ValueError(f"observer conflicts with scope metadata: {field}")
    result = observe_voltage(model_time_s, model_voltage_v, np.asarray(capture["time_s"]), config)
    result["channel"] = channel
    result["nominal_residual_v"] = [
        None if p is None else observed - p
        for observed, p in zip(selected["values_si"], result["voltage_v"], strict=True)
    ]
    result["alignment_status"] = (
        "declared_offset_only"
        if config.alignment_uncertainty_s is None
        else "declared_offset_and_uncertainty"
    )
    result["capture_synchronization"] = capture.get("synchronization", "unestablished")
    result["cross_channel_synchronization_established"] = False
    result["parameter_upgrades"] = []
    return result
