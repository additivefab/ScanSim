"""Research component frames, using only copied probes from the same physical trajectory."""

import math

from zlscan.contracts.time import ClockDomain

COMPONENT_PROBES = {
    "x_m",
    "y_m",
    "line",
    "facet",
    "scan_active",
    "laser_power_w",
    "laser_gate",
    "scene_evaluated",
    "film_width_m",
    "film_height_m",
    "t_eff",
    "power_w",
    "current_a",
    "v_adc",
}


def component_frame(cfg, step, values, code, sample_index, epoch, sample_epoch=None):
    fine = ClockDomain(cfg.domain_id, cfg.fs_hz * cfg.os)
    adc = ClockDomain(cfg.domain_id, cfg.fs_hz)

    def node(key, title, signal, value, unit, status="replayed"):
        return dict(
            id=key,
            title=title,
            signal_id=signal,
            value=value,
            unit=unit,
            status=status,
            visibility="ORACLE_ONLY",
        )

    nodes = [
        node("laser", "光源", "source.power_w", values["laser_power_w"], "W"),
        node("scan", "多面镜扫描", "scan.x_m", values["x_m"], "m"),
        node(
            "film",
            "光斑有效透射率",
            "film.t_eff",
            values["t_eff"] if values["scene_evaluated"] else None,
            "1",
            "replayed" if values["scene_evaluated"] else "not_computed",
        ),
        node("collect", "PMT 入射光", "pmt.power_w", values["power_w"], "W"),
        node("pmt", "PMT 电流模型", "pmt.current_a", values["current_a"], "A"),
        node("afe", "ADC 输入模型", "afe.v_adc", values["v_adc"], "V"),
        {
            **node("adc", "源 ADC 码（保持）", "adc.code", code, "code"),
            "sample_index": str(sample_index),
            "sample_tick_ps": str(adc.tick_of(sample_index)),
            "epoch": epoch if sample_epoch is None else sample_epoch,
            "held": step % cfg.os != 0,
        },
    ]
    geometry = {key: values[key] for key in ("x_m", "y_m", "film_width_m", "film_height_m")}
    geometry = {
        key: float(value) if math.isfinite(value) else None for key, value in geometry.items()
    }
    geometry.update(
        line=str(int(values["line"])),
        facet=int(values["facet"]),
        scan_active=bool(values["scan_active"]),
        laser_gate=bool(values["laser_gate"]),
    )
    return dict(
        status="replayed",
        visibility="ORACLE_ONLY",
        origin="SIMULATED",
        step=str(step),
        tick_ps=str(fine.tick_of(step)),
        epoch=epoch,
        nodes=nodes,
        geometry=geometry,
        parameter_evidence=dict(cfg.evidence),
        semantics="simultaneous model observation; ADC source hold uses its own edge",
        unmodelled_ports=dict(
            log_board_output=None,
            main_amplifier_output=None,
            adc_module_input=None,
            physical_optical_path_length=None,
        ),
    )
