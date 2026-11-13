"""Signal registry for the forward chain (S3-T12): what each probe is and who may see it.

Optical and internal analogue nodes are simulation truth (``ORACLE_ONLY``); only the ADC codes
are something a real instrument delivers (``OBSERVABLE``). Probes are read-only copies; they
never touch the generator or the module state.
"""

from __future__ import annotations

from zlscan.contracts.events import Derivation, Origin, Visibility
from zlscan.contracts.signals import SignalDecl
from zlscan.engine.chain import MODEL_VERSION, ChainConfig


def _decl(cfg: ChainConfig, sid: str, comp: str, domain: str, quantity: str, unit: str,
          visible: Visibility, policy: str) -> SignalDecl:  # fmt: skip
    evidence = ",".join(sorted(set(cfg.evidence.values()))) or "unstated"
    return SignalDecl(
        signal_id=sid, component_id=comp, port_id="out", physical_domain=domain,  # type: ignore[arg-type]
        quantity=quantity, unit=unit, value_type="vector", origin=Origin.SIMULATED,
        visibility=visible, derivation=Derivation.INFERRED, parameter_evidence=evidence,
        model_id=f"truth.{comp}", model_version=MODEL_VERSION, clock_domain=cfg.domain_id,
        native_step_policy=("ADC sample period 1/fs" if sid == "adc.code"
                            else f"fine step 1/(fs*{cfg.os})"),
        display_interpolation="hold" if sid == "adc.code" else "none",
        recording_policy=policy,  # type: ignore[arg-type]
    )  # fmt: skip


def chain_signals(cfg: ChainConfig) -> list[SignalDecl]:
    o, v = Visibility.ORACLE_ONLY, Visibility.OBSERVABLE
    return [
        _decl(cfg, "scan.x_m", "scan", "mechanical", "beam position", "m", o, "on_demand"),
        _decl(cfg, "film.t_eff", "psf", "optical", "effective transmittance", "1", o, "summary"),
        _decl(cfg, "pmt.power_w", "collect", "optical", "power on the PMT", "W", o, "summary"),
        _decl(cfg, "pmt.current_a", "detector", "analog", "anode current", "A", o, "summary"),
        _decl(cfg, "afe.v_adc", "afe", "analog", "voltage at the ADC input", "V", o, "summary"),
        _decl(cfg, "adc.code", "adc", "digital", "ADC code", "1", v, "full"),
        _decl(cfg, "scan.y_m", "scan", "mechanical", "film feed position", "m", o, "on_demand"),
        _decl(cfg, "scan.facet", "scan", "mechanical", "polygon facet", "1", o, "on_demand"),
        _decl(
            cfg, "scan.active", "scan", "digital", "geometric active window", "1", o, "on_demand"
        ),
        _decl(
            cfg, "source.power_w", "source", "optical", "laser output power", "W", o, "on_demand"
        ),
        _decl(cfg, "source.gate", "source", "digital", "laser gate", "1", o, "on_demand"),
    ]


PROBE_TO_SIGNAL = {
    "x_m": "scan.x_m", "t_eff": "film.t_eff", "power_w": "pmt.power_w",
    "current_a": "pmt.current_a", "v_adc": "afe.v_adc",
    "y_m": "scan.y_m", "facet": "scan.facet", "scan_active": "scan.active",
    "laser_power_w": "source.power_w", "laser_gate": "source.gate",
}  # fmt: skip
