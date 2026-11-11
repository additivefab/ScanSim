#!/usr/bin/env python3
"""Review helpers only: arithmetic, evidence and hardware-profile gates.
Not an optical/electronic scanner simulator; no hardware is accessed.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any


def positive_number(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f'{name} must be a positive finite number')
    return float(value)


def bandwidth(fs_hz: float, channels: int, stored_bytes: int = 2,
              keep_fraction: float = 1.0, decimation: int = 1,
              overhead_fraction: float = 0.0, output_Bps: float | None = None) -> dict[str, Any]:
    fs_hz = positive_number(fs_hz, 'fs_hz')
    for name, x in [('channels', channels), ('stored_bytes', stored_bytes), ('decimation', decimation)]:
        if isinstance(x, bool) or not isinstance(x, int) or x <= 0:
            raise ValueError(f'{name} must be a positive integer')
    if not math.isfinite(keep_fraction) or not 0 <= keep_fraction <= 1:
        raise ValueError('keep_fraction must be in [0, 1]')
    if not math.isfinite(overhead_fraction) or overhead_fraction < 0:
        raise ValueError('overhead_fraction must be non-negative')
    raw = fs_hz * channels * stored_bytes
    kept = raw * keep_fraction / decimation
    transmitted = kept * (1 + overhead_fraction)
    limit = None if output_Bps is None else positive_number(output_Bps, 'output_Bps')
    return {'raw_Bps': raw, 'kept_Bps': kept, 'transmitted_Bps': transmitted,
            'output_Bps': limit,
            'average_gate': 'UNRESOLVED' if limit is None else ('PASS' if transmitted < limit else 'FAIL'),
            'note': 'PASS only means average-rate margin, not burst or hardware verification; equality has no margin.'}


def frame_bytes(width: int, height: int, channels: int, stored_bytes: int) -> int:
    values = (width, height, channels, stored_bytes)
    if any(isinstance(x, bool) or not isinstance(x, int) or x <= 0 for x in values):
        raise ValueError('frame dimensions, channels and stored bytes must be positive integers')
    return width * height * channels * stored_bytes


def queue_trace(events: list[tuple[str, int]], capacity: int) -> dict[str, int]:
    """Process an explicitly ordered arrival/service event trace; drop newest on overflow."""
    if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 0:
        raise ValueError('capacity must be a non-negative integer')
    queued = peak = produced = consumed = lost = 0
    for kind, amount in events:
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise ValueError('event amount must be a non-negative integer')
        if kind == 'arrival':
            produced += amount
            accepted = min(amount, capacity - queued)
            queued += accepted
            lost += amount - accepted
            peak = max(peak, queued)
        elif kind == 'service':
            sent = min(queued, amount)
            queued -= sent
            consumed += sent
        else:
            raise ValueError(f'unknown event kind: {kind}')
    assert produced == consumed + lost + queued
    return dict(produced=produced, consumed=consumed, lost=lost, queued=queued, peak=peak)


def measurement_gate(channels_required: int, channels_available: int, simultaneous: bool = True) -> str:
    if channels_required <= 0 or channels_available <= 0:
        raise ValueError('channel counts must be positive')
    if simultaneous and channels_required > channels_available:
        return 'FAIL'
    return 'PASS' if simultaneous else 'NON_SIMULTANEOUS'


def observable_event_gate(event: dict[str, Any]) -> str:
    allowed = {'type','sample_index','clock_domain_id','epoch','observability','origin',
               'uncertainty','measured_value','inferred_value'}
    if set(event) - allowed:
        return 'FAIL'
    if event.get('observability') not in ('measured','inferred'):
        return 'FAIL'
    if not event.get('origin'):
        return 'UNRESOLVED'
    return 'PASS'


def hardware_gate(config: dict[str, Any]) -> dict[str, Any]:
    hw = config['hardware']; b=hw['board']; a=hw['adc_module']; t=hw['transport']; c=hw['control']
    missing=[]
    for obj, key, label in [
        (b,'assembly_id','目标装配实例'),(b,'vccio_bank33_measured_v','BANK33 VADJ实测值'),
        (b,'fpga_speed_grade','FPGA速度等级'),(b,'ddr_reserved_bytes','板端DDR保留量'),
        (a,'actual_conversion_rate_hz','实际ADC转换速率'),(a,'clock_source','ADC时钟源'),
        (a,'digital_interface_mode','ADC输出模式'),(a,'connector_logic_voltage_v','模块排针电平'),
        (a,'level_shifter_details','电平转换网络'),(a,'register_dump','ADC寄存器读回'),
        (t,'mac_route','网口逻辑路线'),(t,'measured_payload_bytes_per_second','有效出口速率'),
        (c,'firmware_hash','固件版本')]:
        if obj.get(key) is None:missing.append(label)
    if not a.get('pin_mapping_verified'):missing.append('ADC到FPGA的已核对网络')
    if not c.get('sos_source_verified'):missing.append('SOS实际来源')
    return {'status':'UNRESOLVED' if missing else 'READY_FOR_FURTHER_VALIDATION',
            'missing':missing,
            'note':'该门槛仅检查资料完整性；资料齐全也不等于已通过电气/时序/实机验证。'}


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    args=parser.parse_args(); root=args.root.resolve()
    source_list=json.loads((root/'tables/sources.json').read_text(encoding='utf-8'))
    checks=[]
    for s in source_list:
        if 'sha256' in s:
            p=root/'originals'/s['name']
            checks.append({'source':s['id'],'hash_matches':p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest()==s['sha256']})
    cfg=json.loads((root/'configs/硬件配置草案_未知项禁止自动补齐.json').read_text(encoding='utf-8'))
    rows=[]
    for fs in [20,40,50,125]:
        for channels in [1,2]:
            rows.append({'fs_msps':fs,'channels':channels,**bandwidth(fs*1e6,channels,output_Bps=100e6)})
    pins=json.loads((root/'tables/pin_registry.json').read_text(encoding='utf-8'))
    result={'purpose':'审查辅助规则检查，不是V1/V3仿真验证',
            'source_hashes':checks,'pin_rows':len(pins),'budget_scenarios':rows,
            'output_limit_assumption':'100 MB/s应用出口仅为演示假设；不是实测',
            'frame_uint16_single_B':frame_bytes(7000,8600,1,2),
            'frame_uint16_dual_B':frame_bytes(7000,8600,2,2),
            'frame_float32_single_B':frame_bytes(7000,8600,1,4),
            'hardware_draft_gate':hardware_gate(cfg),
            'scope_three_signals_two_channels':measurement_gate(3,2),
            'not_executed':['V1默认算例','V1原14项测试','V3整幅仿真','RTL协同','SPICE','Vivado综合/时序','实测拟合']}
    out=root/'verification/审查辅助检查结果.json'; out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'source_hashes_ok':all(x['hash_matches'] for x in checks),
                      'pin_rows':len(pins),'hardware_gate':result['hardware_draft_gate']['status'],
                      'missing_hardware_items':len(result['hardware_draft_gate']['missing']),
                      'result_file':str(out)},ensure_ascii=False,indent=2))
    return 0 if all(x['hash_matches'] for x in checks) else 1

if __name__ == '__main__':
    raise SystemExit(main())
