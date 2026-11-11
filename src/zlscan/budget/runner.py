"""Feasibility budget orchestration: inputs -> conclusions -> ``budget.json`` and an offline page.

Inputs are the unified parameter table overlaid by a config file. A config input is
``{"value": ..., "evidence": "assumed"}``. Keys that are not in the table carry the prefix
``budget.`` (analysis choices such as samples per pixel). Nothing is ever defaulted: an input
that is ``None`` makes every conclusion that needs it "无法计算".

Modes: ``research`` allows assumed values (a scenario flagged on the first line of the report);
``hardware_constrained`` refuses to run when any input read is missing or only assumed.
"""

from __future__ import annotations

import html
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zlscan.budget.geometry import solve
from zlscan.budget.memory import FrameSpec, board_memory
from zlscan.budget.modes import AcquisitionMode, Stage, assess
from zlscan.budget.photons import PhotonInputs, photon_budget
from zlscan.budget.signals import SignalInputs, signal_budget
from zlscan.contracts.param import Evidence
from zlscan.evidence.table import load_parameters

DEFAULT_DENSITIES = [0.0, 1.0, 2.0, 3.0]
UNCOMPUTABLE = "无法计算"


class BudgetRefused(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Item:
    value: Any
    evidence: str | None


class Inputs:
    """Parameter table overlaid by config inputs; remembers every key that was read."""

    def __init__(self, config_inputs: dict[str, dict[str, Any]] | None = None) -> None:
        self.items: dict[str, Item] = {}
        for key, p in load_parameters().items():
            self.items[key] = Item(p.value, p.evidence.value if p.evidence else None)
        for key, entry in (config_inputs or {}).items():
            value = entry.get("value")
            ev = entry.get("evidence")
            if value is not None and ev is None:
                raise ValueError(f"input {key} has a value but no evidence")
            if ev is not None:
                Evidence(ev)  # reject unknown evidence names
            self.items[key] = Item(value, ev)
        self.read: set[str] = set()

    def num(self, key: str, scale: float = 1.0) -> float | None:
        self.read.add(key)
        item = self.items.get(key)
        if item is None or isinstance(item.value, bool):
            return None
        if not isinstance(item.value, (int, float)):
            return None
        return float(item.value) * scale

    def evidence(self, key: str) -> str | None:
        item = self.items.get(key)
        return item.evidence if item else None


def _deps(inputs: Inputs, keys: list[str]) -> list[dict[str, Any]]:
    return [
        {"key": k, "evidence": inputs.evidence(k), "known": inputs.num(k) is not None} for k in keys
    ]


def _assumed(deps: list[dict[str, Any]]) -> int:
    return sum(1 for d in deps if d["known"] and d["evidence"] == Evidence.ASSUMED.value)


def _conclusion(cid, title, status, value, deps, text, unit=""):
    return {
        "id": cid,
        "title": title,
        "status": status,  # PASS | FAIL | UNRESOLVED | INFO
        "value": value,
        "unit": unit,
        "assumed_inputs": _assumed(deps),
        "depends_on": deps,
        "text": text,
    }


def _missing_text(deps: list[dict[str, Any]]) -> str:
    gone = [d["key"] for d in deps if not d["known"]]
    return f"{UNCOMPUTABLE}：缺少所需输入（未知的有 {', '.join(gone)}）" if gone else UNCOMPUTABLE


GEOMETRY_KEYS = {
    "facets": ("scan.facet_count", 1.0),
    "rpm": ("scan.rpm", 1.0),
    "f_line_hz": ("scan.line_rate_hz", 1.0),
    "focal_m": ("scan.f_theta_focal_mm", 1e-3),
    "pitch_x_m": ("product.pixel_pitch_um", 1e-6),
    "pitch_y_m": ("budget.pitch_y_um", 1e-6),
    "spp": ("budget.spp", 1.0),
    "fs_hz": ("adc.actual_conversion_rate_hz", 1.0),
    "nx": ("budget.nx", 1.0),
    "ny": ("budget.ny", 1.0),
    "theta_max_rad": ("budget.theta_max_rad", 1.0),
}
GEOMETRY_REPORT = [
    ("pixel_rate_hz", "像素率", "Hz"),
    ("fs_hz", "ADC 采样率", "Hz"),
    ("f_line_hz", "行频", "Hz"),
    ("width_m", "扫描宽度", "m"),
    ("duty", "有效占空比", ""),
    ("frame_time_s", "帧时间", "s"),
]


def _geometry(inputs: Inputs, out: dict[str, Any]) -> Any:
    given, ev = {}, {}
    for var, (key, scale) in GEOMETRY_KEYS.items():
        v = inputs.num(key, scale)
        if v is not None:
            given[var] = v
            ev[var] = Evidence(inputs.evidence(key))
    if "pitch_y_m" not in given and "pitch_x_m" in given:
        given["pitch_y_m"] = given["pitch_x_m"]  # square pixels is a stated rule, flagged below
        ev["pitch_y_m"] = ev["pitch_x_m"]
    res = solve(given, ev)
    all_keys = [k for k, _ in GEOMETRY_KEYS.values()]
    deps = _deps(inputs, all_keys)
    for var, title, unit in GEOMETRY_REPORT:
        if var in res.values:
            roots = _roots_keys(var, res)
            d = [x for x in deps if x["key"] in roots]
            note = ""
            if (
                var == "pixel_rate_hz"
                and "pitch_y_m" in given
                and "budget.pitch_y_um" not in {x["key"] for x in deps if x["known"]}
            ):
                note = ""
            out["conclusions"].append(
                _conclusion(f"geometry.{var}", title, "INFO", res.values[var], d, note, unit)
            )
        else:
            out["conclusions"].append(
                _conclusion(
                    f"geometry.{var}", title, "UNRESOLVED", None, deps, _missing_text(deps), unit
                )
            )
    for c in res.conflicts:
        out["conclusions"].append(
            _conclusion(
                "geometry.conflict", f"几何矛盾：{c.variable}", "FAIL", None, deps, c.message
            )
        )
    return res


def _roots_keys(var: str, res: Any) -> set[str]:
    from zlscan.budget.geometry import _roots

    names = _roots((var,), res.provenance)
    reverse = {v: k for v, (k, _) in GEOMETRY_KEYS.items()}
    return {reverse[n] for n in names if n in reverse}


def _photons(inputs: Inputs, geo: Any, out: dict[str, Any], densities: list[float]) -> Any:
    pixel_time = inputs.num("budget.pixel_time_s")
    keys = [
        "laser.power_w", "budget.pre_film_efficiency", "collect.efficiency", "pmt.qe", "pmt.gain",
        "laser.wavelength_nm", "budget.pixel_time_s", "budget.excess_noise_factor",
        "pmt.linear_anode_current_max_a",
    ]  # fmt: skip
    if pixel_time is None and "pixel_rate_hz" in geo.values:
        pixel_time = 1 / geo.values["pixel_rate_hz"]
    inp = PhotonInputs(
        laser_w=inputs.num("laser.power_w"),
        pre_film_efficiency=inputs.num("budget.pre_film_efficiency"),
        collection_efficiency=inputs.num("collect.efficiency"),
        qe=inputs.num("pmt.qe"),
        gain=inputs.num("pmt.gain"),
        wavelength_nm=inputs.num("laser.wavelength_nm"),
        pixel_time_s=pixel_time,
        excess_noise_factor=inputs.num("budget.excess_noise_factor"),
        anode_limit_a=inputs.num("pmt.linear_anode_current_max_a"),
    )
    deps = _deps(inputs, keys)
    r = photon_budget(inp, densities)
    if r.missing:
        out["conclusions"].append(
            _conclusion(
                "photons.electrons",
                "每像素光电子数",
                "UNRESOLVED",
                None,
                deps,
                f"{UNCOMPUTABLE}：缺少 {', '.join(r.missing)}",
            )  # fmt: skip
        )
        return r
    for row in r.rows:
        out["conclusions"].append(
            _conclusion(
                f"photons.electrons.D{row.density:g}",
                f"D={row.density:g} 每像素光电子数",
                "INFO",
                row.electrons_per_pixel,
                deps,
                "",
                "e-",
            )  # fmt: skip
        )
        out["conclusions"].append(
            _conclusion(
                f"photons.anode.D{row.density:g}",
                f"D={row.density:g} 阳极电流",
                "FAIL" if row.over_limit else ("PASS" if row.over_limit is False else "INFO"),
                row.anode_current_a,
                deps,
                "超过线性上限" if row.over_limit else "",
                "A",
            )  # fmt: skip
        )
        if row.sigma_density is None:
            out["conclusions"].append(
                _conclusion(
                    f"photons.sigma.D{row.density:g}",
                    f"D={row.density:g} 散粒噪声密度",
                    "UNRESOLVED",
                    None,
                    deps,
                    f"{UNCOMPUTABLE}：缺少 budget.excess_noise_factor",
                )  # fmt: skip
            )
        else:
            note = "" if row.high_count_valid else "低计数，高计数近似不适用"
            out["conclusions"].append(
                _conclusion(
                    f"photons.sigma.D{row.density:g}",
                    f"D={row.density:g} 散粒噪声密度",
                    "INFO",
                    row.sigma_density,
                    deps,
                    note,
                    "OD",
                )  # fmt: skip
            )
    return r


def _signals(inputs: Inputs, photons: Any, out: dict[str, Any], densities: list[float]) -> None:
    keys = [
        "budget.log_offset_v", "budget.log_slope_v_per_decade", "budget.log_reference_current_a",
        "budget.polarity", "budget.driver_gain", "budget.driver_offset_v", "budget.adc_vmin",
        "budget.adc_vmax", "adc.module_silkscreen_bits", "pmt.dark_current_a",
    ]  # fmt: skip
    deps = _deps(inputs, keys)
    clear = (
        photons.rows[0].anode_current_a if photons.rows and photons.rows[0].density == 0 else None
    )
    pol = inputs.num("budget.polarity")
    bits = inputs.num("adc.module_silkscreen_bits")
    inp = SignalInputs(
        log_offset_v=inputs.num("budget.log_offset_v"),
        log_slope_v_per_decade=inputs.num("budget.log_slope_v_per_decade"),
        log_reference_current_a=inputs.num("budget.log_reference_current_a"),
        polarity=None if pol is None else int(pol),
        driver_gain=inputs.num("budget.driver_gain"),
        driver_offset_v=inputs.num("budget.driver_offset_v"),
        adc_vmin=inputs.num("budget.adc_vmin"),
        adc_vmax=inputs.num("budget.adc_vmax"),
        adc_bits=None if bits is None else int(bits),
        clear_current_a=clear,
        dark_current_a=inputs.num("pmt.dark_current_a"),
    )
    r = signal_budget(inp, densities)
    if r.missing:
        out["conclusions"].append(
            _conclusion(
                "signals.levels",
                "前端电平与 ADC 量程",
                "UNRESOLVED",
                None,
                deps,
                f"{UNCOMPUTABLE}：缺少 {', '.join(r.missing)}",
            )  # fmt: skip
        )
        return
    for row in r.rows:
        out["conclusions"].append(
            _conclusion(
                f"signals.adc_v.D{row.density:g}",
                f"D={row.density:g} ADC 输入电压",
                "PASS" if row.in_adc_range else "FAIL",
                row.adc_v,
                deps,
                "" if row.in_adc_range else "超出 ADC 量程",
                "V",
            )  # fmt: skip
        )
    out["conclusions"].append(
        _conclusion(
            "signals.density_step",
            "每 ADC 码对应的密度步长",
            "INFO",
            r.density_step_per_code,
            deps,
            "二级主放与模块输入网络未决，见各测点说明",
            "OD",
        )  # fmt: skip
    )


def _datapath(inputs: Inputs, geo: Any, cfg: dict[str, Any], out: dict[str, Any]) -> None:
    mode = AcquisitionMode(cfg.get("mode", "finite_frame"))
    fs = geo.values.get("fs_hz")
    ch = inputs.num("budget.channels")
    bits = inputs.num("adc.module_silkscreen_bits")
    keys = [
        "adc.actual_conversion_rate_hz", "budget.channels", "adc.module_silkscreen_bits",
        "transport.measured_payload_bytes_per_second", "board.ddr_reserved_bytes",
    ]  # fmt: skip
    if fs is not None:
        keys = [k for k in keys if k != "adc.actual_conversion_rate_hz"]
        keys += sorted(_roots_keys("fs_hz", geo))
    deps = _deps(inputs, keys)
    stages = []
    for s in cfg.get("stages", []):
        stages.append(Stage(s["name"], s.get("capacity_bytes"), s.get("service_Bps"),
                            tuple(tuple(x) for x in s.get("stalls", []))))  # fmt: skip
    if not stages:
        ddr, resv = inputs.num("board.ddr_physical_bytes"), inputs.num("board.ddr_reserved_bytes")
        cap = None if ddr is None or resv is None else ddr - resv
        stages = [
            Stage("transport", cap, inputs.num("transport.measured_payload_bytes_per_second"))
        ]
    if fs is None or ch is None or bits is None:
        out["conclusions"].append(
            _conclusion(
                "datapath.mode",
                f"数据通路（{mode.value}）",
                "UNRESOLVED",
                None,
                deps,
                _missing_text(deps),
            )  # fmt: skip
        )
        return
    input_bps = fs * ch * math.ceil(bits / 8)
    duration = cfg.get("duration_s") or geo.values.get("frame_time_s")
    if mode is not AcquisitionMode.CONTINUOUS and duration is None:
        out["conclusions"].append(
            _conclusion(
                "datapath.mode",
                f"数据通路（{mode.value}）",
                "UNRESOLVED",
                None,
                deps,
                f"{UNCOMPUTABLE}：缺少帧时间或 datapath.duration_s",
            )  # fmt: skip
        )
        return
    r = assess(mode, input_bps, stages, duration, cfg.get("drain_limit_s"), cfg.get("period_s"))
    peaks = {s.name: s.peak_backlog_bytes for s in r.stages}
    out["conclusions"].append(
        _conclusion(
            "datapath.mode",
            f"数据通路（{mode.value}）",
            r.verdict.value,
            input_bps,
            deps,
            r.reason + (f"；峰值积压 {peaks}" if peaks else ""),
            "B/s",
        )  # fmt: skip
    )


def _memory(inputs: Inputs, geo: Any, out: dict[str, Any]) -> None:
    keys = [
        "benchmark.width_pixels", "benchmark.height_pixels", "budget.channels",
        "adc.module_silkscreen_bits", "budget.buffers", "board.ddr_physical_bytes",
        "board.ddr_reserved_bytes",
    ]  # fmt: skip
    deps = _deps(inputs, keys)
    vals = [inputs.num(k) for k in keys]
    nx, ny, ch, bits, bufs, ddr, resv = vals
    if None in (nx, ny, ch, bits, bufs, ddr):
        out["conclusions"].append(
            _conclusion("memory.board", "板端内存", "UNRESOLVED", None, deps, _missing_text(deps))
        )
        return
    frame = FrameSpec("raw", int(nx), int(ny), int(ch), math.ceil(bits / 8), int(bufs))
    r = board_memory([frame], resv, ddr)
    out["conclusions"].append(
        _conclusion(
            "memory.board", "板端内存", r.verdict.value, r.total_bytes, deps, r.reason, "B"
        )  # fmt: skip
    )


def run_budget(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config or {}
    mode = config.get("mode", "research")
    if mode not in ("research", "hardware_constrained"):
        raise ValueError(f"unknown mode {mode!r}")
    inputs = Inputs(config.get("inputs"))
    densities = config.get("densities", DEFAULT_DENSITIES)
    out: dict[str, Any] = {
        "schema": "zlscan.budget/1",
        "mode": mode,
        "scenario": config.get("scenario"),
        "conclusions": [],
    }
    geo = _geometry(inputs, out)
    photons = _photons(inputs, geo, out, densities)
    _signals(inputs, photons, out, densities)
    _datapath(inputs, geo, config.get("datapath", {}), out)
    _memory(inputs, geo, out)

    if mode == "hardware_constrained":
        problems = []
        for k in sorted(inputs.read):
            item = inputs.items.get(k)
            if item is None or item.value is None:
                problems.append(f"{k}: 缺少资料")
            elif item.evidence == Evidence.ASSUMED.value:
                problems.append(f"{k}: 仅为假设")
        if problems:
            raise BudgetRefused(problems)
    assumed = sorted(
        k for k in inputs.read
        if inputs.items.get(k) and inputs.items[k].value is not None
        and inputs.items[k].evidence == Evidence.ASSUMED.value
    )  # fmt: skip
    out["assumed_inputs"] = assumed
    out["flagged_assumed_scenario"] = bool(assumed) or bool(
        (config.get("scenario") or {}).get("assumed")
    )
    return out


_STATUS_CLASS = {"PASS": "ok", "FAIL": "bad", "UNRESOLVED": "gap", "INFO": "info"}


def render_report(budget: dict[str, Any]) -> str:
    rows = []
    for c in budget["conclusions"]:
        value = "" if c["value"] is None else f"{c['value']:.6g} {c['unit']}"
        rows.append(
            f"<tr class='{_STATUS_CLASS.get(c['status'], 'info')}'>"
            f"<td>{html.escape(c['title'])}</td>"
            f"<td>{html.escape(c['status'])}</td><td>{html.escape(value)}</td>"
            f"<td>{c['assumed_inputs']}</td><td>{html.escape(c['text'])}</td></tr>"
        )
    banner = ""
    if budget["flagged_assumed_scenario"]:
        scenario = budget.get("scenario") or {}
        banner = (
            "<p class='banner'>假设场景：以下数字依赖假设输入，不是硬件事实。"
            f"{html.escape(str(scenario.get('name', '')))} "
            f"{html.escape(str(scenario.get('note', '')))}</p>"
            f"<p>假设输入：{html.escape(', '.join(budget['assumed_inputs']) or '无')}</p>"
        )
    dep_rows = "".join(
        f"<tr><td>{html.escape(c['title'])}</td><td>"
        + html.escape(", ".join(f"{d['key']}[{d['evidence'] or '未知'}]" for d in c["depends_on"]))
        + "</td></tr>"
        for c in budget["conclusions"]
    )
    return (
        "<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
        "<title>zlscan 可行性预算</title><style>"
        "body{font:14px sans-serif;margin:24px}table{border-collapse:collapse}"
        "td,th{border:1px solid #888;padding:3px 8px}.banner{background:#fc8;padding:8px}"
        ".bad{background:#fcc}.gap{background:#eee}.ok{background:#cfc}</style></head><body>"
        f"<h1>可行性预算（{html.escape(budget['mode'])}）</h1>{banner}"
        "<p>PASS 仅表示该预算项在所给输入下成立，不代表实机通过；"
        "UNRESOLVED 表示缺少资料，没有用默认值补数。</p>"
        "<table><tr><th>结论</th><th>状态</th><th>数值</th><th>依赖的假设输入数</th>"
        f"<th>说明</th></tr>{''.join(rows)}</table>"
        f"<h2>依赖与证据</h2><table>{dep_rows}</table></body></html>"
    )


def write_outputs(budget: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    j = out_dir / "budget.json"
    j.write_text(json.dumps(budget, ensure_ascii=False, indent=2), encoding="utf-8")
    h = out_dir / "budget_report.html"
    h.write_text(render_report(budget), encoding="utf-8")
    return j, h
