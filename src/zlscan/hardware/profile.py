"""Hardware profile gate: is the information needed to run in ``hardware_constrained`` mode there?

The gate checks *completeness of data* only. "Ready for further validation" is deliberately not
called PASS: complete data does not mean the electrical, timing or hardware verification has been
done (docs/dev/07_evidence_rules.md, section 5).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from zlscan.contracts.param import Evidence, Param


class ProfileStatus(StrEnum):
    UNRESOLVED = "UNRESOLVED"
    READY_FOR_FURTHER_VALIDATION = "READY_FOR_FURTHER_VALIDATION"


# The 15 items the review package found missing for the hardware profile, as parameter keys.
REQUIRED_FOR_HARDWARE: tuple[tuple[str, str], ...] = (
    ("board.assembly_id", "装配实例编号"),
    ("board.vccio_bank33_measured_v", "J6（BANK33，VADJ）的实测电压"),
    ("board.fpga_speed_grade", "FPGA 速度等级"),
    ("board.ddr_reserved_bytes", "DDR 中系统与 DMA 的保留量"),
    ("adc.actual_conversion_rate_hz", "ADC 实际转换速率"),
    ("adc.clock_source", "ADC 时钟来源"),
    ("adc.digital_interface_mode", "数字输出模式（CMOS、LVDS）"),
    ("adc.digital_output_level_v", "模块连接器的数字电平"),
    ("adc.level_shifter", "模块的电平转换"),
    ("adc.register_dump", "ADC 寄存器读回"),
    ("transport.mac_route", "MAC 路线（PS 经 EMIO 或 PL MAC）"),
    ("transport.measured_payload_bytes_per_second", "实测有效载荷速率"),
    ("control.firmware_hash", "固件校验值"),
    ("hardware.wiring_carrier_module_to_board", "载板与模块到开发板的接线（已核对）"),
    ("control.sos_source", "行同步信号的真实来源"),
)

NOTE = "该门槛只检查资料完整性；资料齐全也不等于已通过电气、时序或实机验证。"


@dataclass(frozen=True)
class ProfileGate:
    status: ProfileStatus
    missing: list[str] = field(default_factory=list)
    note: str = NOTE


def _satisfied(param: Param | None) -> bool:
    """A value counts only if it is known, not a failed verification flag, and not an assumption."""
    return (
        param is not None
        and param.value is not None
        and param.value is not False
        and param.evidence is not Evidence.ASSUMED
    )


def profile_gate(params: Mapping[str, Param]) -> ProfileGate:
    missing = [label for key, label in REQUIRED_FOR_HARDWARE if not _satisfied(params.get(key))]
    status = ProfileStatus.UNRESOLVED if missing else ProfileStatus.READY_FOR_FURTHER_VALIDATION
    return ProfileGate(status=status, missing=missing)
