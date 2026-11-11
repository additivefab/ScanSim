"""Merge the three parameter tables into src/zlscan/evidence/parameters.json (task S0-T05).

Sources, all frozen under legacy/:
  A  package_a/docs/参数证据与测量清单.csv      (19 rows)
  B  package_b/templates/实测参数记录表.csv      (16 rows)
  R  review/tables/requirements.json              (8 requirements)
  D  review/configs/硬件配置草案_未知项禁止自动补齐.json (hardware draft; every leaf)

Rules baked in here:
* a parameter is unknown (value None, no evidence) unless a document or observation really
  states it; the packages' example values are copied into ``legacy_examples`` only;
* texts (examples, how to obtain, effect) are copied from the source rows, never retyped;
* every source row and every draft leaf must be consumed, or generation fails.

Run ``python tools/build_parameter_table.py`` to write the file, ``--check`` to verify that the
committed file is current.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any

from zlscan.contracts.param import Evidence, Param

ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "legacy"
OUTPUT = ROOT / "src" / "zlscan" / "evidence" / "parameters.json"

PATH_A = "package_a/docs/参数证据与测量清单.csv"
PATH_B = "package_b/templates/实测参数记录表.csv"
PATH_R = "review/tables/requirements.json"
PATH_D = "review/configs/硬件配置草案_未知项禁止自动补齐.json"

# Draft leaves that are not parameters (with the reason), so nothing is dropped silently.
DRAFT_NOT_PARAMETERS = {
    "schema_version": "version tag of the draft file",
    "mode": "mode tag of the draft file",
    "experiment.timeline": "runtime object (an empty list), not a parameter",
    "benchmark.source": "provenance text, folded into the benchmark parameters' source_id",
    "explicit_non_facts": "moved to forbidden_defaults.json",
}


def known(value: Any, evidence: Evidence, source: str, locator: str, **extra: Any) -> dict:
    return {
        "value": value,
        "evidence": evidence,
        "source_id": source,
        "locator": locator,
        "status": extra.pop("status", "confirmed"),
        **extra,
    }


# fmt: off
# (key, name, category, unit, A rows, B rows, R ids, D leaves, known-value dict or None)
# A/B rows are the exact first-column strings of the source tables.
A1, A2, A3, A4, A5 = "激光波长/功率/模式", "入射光束直径/M²/偏振", "转镜面数/转速", "等效焦距/扫描映射", "镜面角误差/面间增益"
A6, A7, A8, A9 = "光斑PSF/景深", "底片尺寸/目标像素间距", "送片运动/编码器", "接收集光效率/杂散光"
A10, A11, A12, A13 = "PMT具体型号/高压/分压链", "PMT线性阳极电流", "一级对数放大器", "二级增益/偏置/ADC驱动"
A14, A15, A16, A17 = "ADC模块", "ADC满量程/码制/时钟模式", "ADC流水线/DCO", "GPIO电平/Bank VCCO"
A18, A19 = "SOS具体来源和极性", "存储/传输"
B1, B2, B3, B4 = "扫描镜类型/反射面数", "f-theta焦距", "激光波长/功率", "光斑强度PSF"
B5, B6, B7, B8 = "PMT型号/增益", "PMT暗电流/阳极电流限制", "对数电路型号/极性", "对数斜率/基准电流"
B9, B10, B11, B12 = "最弱光动态响应", "ADC模块", "模块SMA输入耦合/增益/量程", "ADC数字输出电平/格式"
B13, B14, B15, B16 = "ADC时钟/延迟", "SOS极性/到有效区延迟", "送片速度/编码器比例", "几何/密度校准片"
P, H, F, M = Evidence.PHOTO, Evidence.SCHEMATIC, Evidence.ASSUMED, Evidence.MEASURED

ENTRIES = [
    # ---- laser, scan, optics ------------------------------------------------------------
    ("laser.wavelength_nm", "激光波长", "optics", "nm", [A1], [B3], [], [], None),
    ("laser.power_w", "激光功率", "optics", "W", [A1], [B3], [], [], None),
    ("laser.emission_mode", "激光发光方式（连续、门控、脉冲）", "optics", "", [A1], [], [], [], None),
    ("laser.beam_diameter_m2_polarization", "入射光束直径、M²、偏振", "optics", "", [A2], [], [], [], None),
    ("scan.mirror_type", "扫描镜类型", "scan", "", [], [B1], [], [], None),
    ("scan.facet_count", "转镜反射面数", "scan", "", [A3], [B1], [], [], None),
    ("scan.rpm", "转镜转速", "scan", "rpm", [A3], [], [], [], None),
    ("scan.line_rate_hz", "行频", "scan", "Hz", [], [], [], [], None),
    ("scan.line_period_s", "行周期", "scan", "s", [], [], [], [], None),
    ("scan.f_theta_focal_mm", "f-theta 等效焦距", "scan", "mm", [A4], [B2], [], [], None),
    ("scan.mapping", "扫描映射 x(θ, 面)", "scan", "", [A4], [], [], [], None),
    ("scan.facet_errors", "镜面角误差与面间增益", "scan", "", [A5], [], [], [], None),
    ("scan.optics_unit_origin", "扫描光学单元的来源机型", "scan", "", [], [], [], [], None),
    ("optics.spot_psf", "光斑强度分布 PSF", "optics", "µm", [A6], [B4], [], [], None),
    ("optics.depth_of_field", "景深", "optics", "µm", [A6], [], [], [], None),
    ("collect.efficiency", "接收集光效率", "optics", "", [A9], [], [], [], None),
    ("collect.stray_light", "杂散光", "optics", "", [A9], [], [], [], None),
    ("collect.structure", "片后集光结构", "optics", "", [], [], [], [], None),
    # ---- film, feed, product ------------------------------------------------------------
    ("product.film_size_mm", "底片宽、高", "product", "mm", [A7], [], ["R01"],
     ["experiment.product_width_mm", "experiment.product_height_mm"], None),
    ("product.pixel_pitch_um", "输出像素间距", "product", "µm", [A7], [], ["R02"], ["experiment.pixel_pitch_um"], None),
    ("product.density_range_and_tolerance", "有效密度范围与允许偏差", "product", "OD", [], [], ["R03"],
     ["experiment.density_target"], None),
    ("product.scan_and_transfer_time_s", "单幅扫描与传输时长", "product", "s", [], [], ["R04"],
     ["experiment.scan_time_target_s"], None),
    ("product.host_output_policy", "上位机输出与数据保留策略", "product", "", [], [], ["R08"], [], None),
    ("feed.speed_mm_s", "送片速度", "scan", "mm/s", [A8], [B15], [], [], None),
    ("feed.encoder_counts_per_mm", "编码器比例", "scan", "count/mm", [A8], [B15], [], [], None),
    ("calibration.reference_films", "可追溯的几何与密度校准片", "calibration", "", [], [B16], [], [], None),
    # ---- PMT ----------------------------------------------------------------------------
    ("pmt.model", "PMT 型号", "detector", "", [A10], [B5], [], [], None),
    ("pmt.gain", "PMT 增益", "detector", "", [], [B5], [], [], None),
    ("pmt.qe", "PMT 量子效率", "detector", "", [], [], [], [], None),
    ("pmt.hv_and_divider", "PMT 高压模块与分压链", "detector", "", [A10], [], [], [], None),
    ("pmt.linear_anode_current_max_a", "PMT 线性阳极电流上限", "detector", "A", [A11], [B6], [], [], None),
    ("pmt.dark_current_a", "PMT 暗电流", "detector", "A", [], [B6], [], [], None),
    # ---- analogue front end -------------------------------------------------------------
    ("afe.log_amp_device", "对数放大器型号", "afe", "", [A12], [B7], [], [], None),
    ("afe.log_polarity", "对数输出极性", "afe", "", [], [B7], [], [], None),
    ("afe.log_slope_and_reference_current", "对数斜率与基准电流", "afe", "V/dec, A", [], [B8], [], [], None),
    ("afe.weak_light_dynamic_response", "最弱光下的动态响应", "afe", "Hz", [], [B9], [], [], None),
    ("afe.second_stage_gain_offset", "二级增益、偏置与 ADC 驱动", "afe", "", [A13], [], [], [], None),
    ("afe.output_swing_v", "前端输出摆幅", "afe", "V", [], [], [], [], None),
    # ---- ADC module ---------------------------------------------------------------------
    ("adc.module_silkscreen_name", "ADC 模块丝印名称", "adc", "", [A14], [B10], [],
     ["hardware.adc_module.silkscreen_name"],
     known("ADC_AD9268_V4.2", P, "S02", "图4", valid_for="模块丝印；芯片等级未核对")),
    ("adc.module_silkscreen_bits", "ADC 模块丝印位数", "adc", "bit", [], [], [],
     ["hardware.adc_module.silkscreen_bits"], known(16, P, "S02", "图4")),
    ("adc.module_silkscreen_channels", "ADC 模块丝印通道数", "adc", "", [], [], [],
     ["hardware.adc_module.silkscreen_channels"], known(2, P, "S02", "图4")),
    ("adc.module_silkscreen_max_msps", "ADC 模块丝印标称采样率", "adc", "MSPS", [], [], [],
     ["hardware.adc_module.silkscreen_max_msps"],
     known(125, P, "S02", "图4", valid_for="丝印上的标称能力，不是实测工作点")),
    ("adc.chip_speed_grade", "ADC 芯片速度等级", "adc", "MSPS", [], [], [], [], None),
    ("adc.actual_conversion_rate_hz", "ADC 实际转换速率", "adc", "Hz", [A16], [B13], ["R05"],
     ["hardware.adc_module.actual_conversion_rate_hz"], None),
    ("adc.clock_source", "ADC 时钟来源", "adc", "", [], [B13], [], ["hardware.adc_module.clock_source"], None),
    ("adc.pipeline_latency_cycles", "ADC 流水线延迟", "adc", "cycle", [A16], [B13], [], [], None),
    ("adc.dco_relation", "数据时钟与采样时钟的关系", "adc", "", [A16], [], [], [], None),
    ("adc.full_scale_v", "ADC 满量程", "adc", "V", [A15], [], [], [], None),
    ("adc.coding", "ADC 输出码制", "adc", "", [A15], [], [], [], None),
    ("adc.clock_mode", "ADC 时钟模式", "adc", "", [A15], [], [], [], None),
    ("adc.input_coupling", "模块输入的耦合方式", "adc", "", [], [B11], [], [], None),
    ("adc.module_input_gain_range", "模块输入的增益与量程", "adc", "", [], [B11], [], [], None),
    ("adc.input_vpp_v", "ADC 输入峰峰值", "adc", "V", [], [], [], [], None),
    ("adc.digital_interface_mode", "数字输出模式（CMOS、LVDS）", "adc", "", [], [B12], ["R05"],
     ["hardware.adc_module.digital_interface_mode"], None),
    ("adc.digital_output_level_v", "模块连接器的数字电平", "adc", "V", [], [B12], [],
     ["hardware.adc_module.connector_logic_voltage_v"], None),
    ("adc.level_shifter", "模块的电平转换", "adc", "", [], [], [], ["hardware.adc_module.level_shifter_details"], None),
    ("adc.register_dump", "ADC 寄存器读回", "adc", "", [], [], [], ["hardware.adc_module.register_dump"], None),
    ("adc.channel_role_a", "ADC 通道 A 的用途", "adc", "", [], [], [], ["hardware.adc_module.channel_roles.A"], None),
    ("adc.channel_role_b", "ADC 通道 B 的用途", "adc", "", [], [], [], ["hardware.adc_module.channel_roles.B"], None),
    # ---- development board --------------------------------------------------------------
    ("board.family", "开发板系列", "hardware", "", [], [], [], ["hardware.board.family"],
     known("Smart ZYNQ SP2", P, "S03", "正面丝印")),
    ("board.revision", "开发板版本", "hardware", "", [], [], [], ["hardware.board.revision", "hardware.board.revision_evidence"],
     known("V1.3B", P, "S03", "正面丝印", valid_for="独立开发板照片；是否属于目标装配实例待确认")),
    ("board.assembly_id", "装配实例编号", "hardware", "", [], [], [], ["hardware.board.assembly_id"], None),
    ("board.fpga_schematic_part", "原理图中的 FPGA 型号", "hardware", "", [], [], [],
     ["hardware.board.fpga_schematic_part"], known("XC7Z020_CLG484", H, "S05", "第1页")),
    ("board.fpga_speed_grade", "FPGA 速度等级", "hardware", "", [], [], [], ["hardware.board.fpga_speed_grade"], None),
    ("board.vccio_bank35_v", "J5（BANK35）的 VCCIO", "hardware", "V", [A17], [], [],
     ["hardware.board.vccio_bank35_v"], known(3.3, H, "S05", "第10页")),
    ("board.vccio_bank33_measured_v", "J6（BANK33，VADJ）的实测电压", "hardware", "V", [], [], [],
     ["hardware.board.vccio_bank33_measured_v"], None),
    ("board.pl_oscillator_nominal_hz", "PL 晶振标称频率", "hardware", "Hz", [], [], [],
     ["hardware.board.pl_oscillator_nominal_hz"], known(50000000, H, "S05", "第7页")),
    ("board.ps_oscillator_nominal_hz", "PS 晶振标称频率", "hardware", "Hz", [], [], [],
     ["hardware.board.ps_oscillator_nominal_hz"], known(33333000, H, "S05", "第7页", valid_for="原理图标称 33.333 MHz")),
    ("board.ddr_physical_bytes", "DDR 物理容量", "hardware", "B", [A19], [], [],
     ["hardware.board.ddr_physical_bytes"], known(536870912, H, "S05", "第1、3页")),
    ("board.ddr_reserved_bytes", "DDR 中系统与 DMA 的保留量", "hardware", "B", [], [], [],
     ["hardware.board.ddr_reserved_bytes"], None),
    # ---- transport ----------------------------------------------------------------------
    ("transport.phy", "网口 PHY", "transport", "", [A19], [], [], ["hardware.transport.phy"],
     known("RTL8211E", H, "S05", "第5页")),
    ("transport.phy_connection", "网口 PHY 的接入位置", "transport", "", [], [], [],
     ["hardware.transport.phy_connection"], known("PL BANK35", H, "S05", "第2、5、11、12页")),
    ("transport.mac_route", "MAC 路线（PS 经 EMIO 或 PL MAC）", "transport", "", [], [], [], ["hardware.transport.mac_route"], None),
    ("transport.measured_payload_bytes_per_second", "实测有效载荷速率", "transport", "B/s", [], [], [],
     ["hardware.transport.measured_payload_bytes_per_second"], None),
    ("transport.service_trace", "DMA、DDR、出口的服务记录", "transport", "", [], [], [], ["hardware.transport.service_trace"], None),
    # ---- control and observability -------------------------------------------------------
    ("control.sos_source", "行同步信号的真实来源", "control", "", [A18], [B14], ["R07"],
     ["hardware.control.sos_source_verified"], None),
    ("control.sos_polarity", "行同步极性", "control", "", [A18], [B14], [], [], None),
    ("control.sos_to_active_delay_s", "行同步到有效区的延迟", "control", "s", [], [B14], [], [], None),
    ("control.dac_route", "载板 DAC 控制的去向", "control", "", [], [], [], ["hardware.control.dac_route"], None),
    ("control.encoder_present", "是否有编码器", "control", "", [], [], ["R07"], ["hardware.control.encoder_present"], None),
    ("control.facet_index_sensor_present", "是否有镜面或每转参考传感器", "control", "", [], [], ["R07"],
     ["hardware.control.facet_index_sensor_present"], None),
    ("control.reference_photodetector_present", "是否装有激光参考探测器", "control", "", [], [], [],
     ["hardware.control.reference_photodetector_present"], None),
    ("control.firmware_hash", "固件校验值", "control", "", [], [], [], ["hardware.control.firmware_hash"], None),
    ("hardware.wiring_carrier_module_to_board", "载板与模块到开发板的接线", "hardware", "", [], [], ["R06"],
     ["hardware.adc_module.pin_mapping_verified"], None),
    # ---- observation (of the one photographed capture) ----------------------------------
    ("observation.scope_model", "照片中的示波器型号", "observation", "", [], [], [],
     ["observation.scope_observed_model"], known("Hantek DSO5102P", P, "S02", "图3")),
    ("observation.scope_analog_channels", "该示波器的模拟通道数", "observation", "", [], [], [],
     ["observation.analog_channels_observed"], known(2, P, "S02", "图3")),
    ("observation.measurement_node", "测点", "observation", "", [], [], [], ["observation.measurement_node"], None),
    ("observation.probe_ratio", "探头倍率", "observation", "", [], [], [], ["observation.probe_ratio"], None),
    ("observation.coupling", "示波器耦合方式", "observation", "", [], [], [], ["observation.coupling"], None),
    ("observation.sample_interval_s", "采样间隔", "observation", "s", [], [], [], ["observation.sample_interval_s"], None),
    ("observation.simultaneous_capture", "多路是否同次同步采集", "observation", "", [], [], [],
     ["observation.simultaneous_capture"], None),
    # ---- benchmark targets (assumptions, not product requirements) ----------------------
    ("benchmark.width_pixels", "性能测试算例的宽度", "benchmark", "px", [], [], [], ["benchmark.width_pixels"],
     known(7000, F, "S01", "§2.3", status="pending", valid_for="软件性能目标，不是产品规格")),
    ("benchmark.height_pixels", "性能测试算例的高度", "benchmark", "px", [], [], [], ["benchmark.height_pixels"],
     known(8600, F, "S01", "§2.3", status="pending", valid_for="软件性能目标，不是产品规格")),
    ("benchmark.wall_time_goal_s", "性能测试算例的耗时目标", "benchmark", "s", [], [], [], ["benchmark.wall_time_goal_s"],
     known(900, F, "S01", "§2.3", status="pending", valid_for="待验证目标，不是承诺")),
    ("benchmark.host_peak_memory_goal_bytes", "性能测试算例的内存目标", "benchmark", "B", [], [], [],
     ["benchmark.host_peak_memory_goal_bytes"],
     known(8000000000, F, "S01", "§2.3", status="pending", valid_for="待验证目标，不是承诺")),
]
# fmt: on

COLUMNS_A = {"example": "包内示例值", "how": "获取方法", "note": "注意", "state": "证据状态"}
COLUMNS_B = {
    "example": "当前证据或示例",
    "how": "测量方法",
    "note": "对仿真的影响",
    "state": "状态",
}


def _read_csv(relative: str) -> dict[str, dict[str, str]]:
    with (LEGACY / relative).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    table = {row["参数"]: row for row in rows}
    assert len(table) == len(rows), f"duplicate parameter names in {relative}"
    return table


def _draft_leaves(obj: Any, prefix: str = "") -> list[str]:
    if isinstance(obj, dict):
        return [
            leaf
            for k, v in obj.items()
            for leaf in _draft_leaves(v, f"{prefix}.{k}" if prefix else k)
        ]
    return [prefix]


def build() -> dict[str, Any]:
    rows_a, rows_b = _read_csv(PATH_A), _read_csv(PATH_B)
    for name, row in rows_b.items():
        assert not row["实测值"].strip(), f"B row {name!r} has a measured value; do not drop it"
    requirements = {r["id"]: r for r in json.loads((LEGACY / PATH_R).read_text(encoding="utf-8"))}
    assert all(r["value"] is None for r in requirements.values()), (
        "a requirement already has a value"
    )
    draft = json.loads((LEGACY / PATH_D).read_text(encoding="utf-8"))
    leaves = set(_draft_leaves(draft)) - set(DRAFT_NOT_PARAMETERS)

    used_a, used_b, used_r, used_d = set(), set(), set(), set()
    entries = []
    for number, (key, name, category, unit, a, b, r, d, kn) in enumerate(ENTRIES, 1):
        sources, examples, how, affects = [], [], [], []
        for table, label, rows, cols, used in (
            ("A", a, rows_a, COLUMNS_A, used_a),
            ("B", b, rows_b, COLUMNS_B, used_b),
        ):
            for row_name in label:
                row = rows[row_name]
                used.add(row_name)
                sources.append({"table": table, "row": row_name})
                for bucket, column in (("example", examples), ("how", how), ("note", affects)):
                    text = row[cols[bucket]].strip()
                    if text:
                        column.append({"table": table, "row": row_name, "text": text})
        for req_id in r:
            used_r.add(req_id)
            sources.append({"table": "R", "row": req_id})
            affects.append({"table": "R", "row": req_id, "text": requirements[req_id]["reason"]})
        for leaf in d:
            assert leaf in leaves, f"unknown or non-parameter draft leaf {leaf!r}"
            used_d.add(leaf)
            sources.append({"table": "D", "row": leaf})
        if kn is None:
            param = Param(unit=unit)
        else:
            param = Param(unit=unit, **kn)
        entries.append(
            {
                "id": f"P{number:03d}",
                "key": key,
                "name": name,
                "category": category,
                "param": param.model_dump(mode="json"),
                "sources": sources,
                "legacy_examples": examples,
                "how_to_obtain": how,
                "affects": affects,
            }
        )
    assert used_a == set(rows_a), f"A rows without a destination: {sorted(set(rows_a) - used_a)}"
    assert used_b == set(rows_b), f"B rows without a destination: {sorted(set(rows_b) - used_b)}"
    assert used_r == set(requirements), (
        f"requirements without a destination: {sorted(set(requirements) - used_r)}"
    )
    assert used_d == leaves, f"draft leaves without a destination: {sorted(leaves - used_d)}"
    keys = [e["key"] for e in entries]
    assert len(keys) == len(set(keys)), "duplicate parameter keys"
    return {
        "format": "zlscan-parameters/1",
        "note": "legacy_examples are the packages' assumed example values, NOT parameter values; "
        "param.value is None wherever no document or observation states the value.",
        "draft_leaves_not_parameters": DRAFT_NOT_PARAMETERS,
        "parameters": entries,
    }


def render(table: dict[str, Any]) -> str:
    return json.dumps(table, ensure_ascii=False, indent=1) + "\n"


def main(argv: list[str]) -> int:
    text = render(build())
    if "--check" in argv:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != text:
            print(
                f"{OUTPUT} is not up to date; run tools/build_parameter_table.py", file=sys.stderr
            )
            return 1
        print("parameters.json is up to date")
        return 0
    OUTPUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(ROOT)}: {len(build()['parameters'])} parameters")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
