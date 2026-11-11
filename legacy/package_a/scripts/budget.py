#!/usr/bin/env python3
"""Analytical production budget; not a full-size analogue simulation."""
import json, math
from pathlib import Path
p=Path(__file__).resolve().parents[1]
c=json.loads((p/'config/production_budget.json').read_text())
nx=math.ceil(c['width_mm']*1000/c['pitch_x_um']);ny=math.ceil(c['height_mm']*1000/c['pitch_y_um'])
fl=c['facets']*c['rpm']/60;active=c['active_fraction']/fl
bpp=math.ceil(c['bits']/8)*c['channels'];n=nx*ny
v=c['width_mm']*1e-3/active;f=v/(2*2*math.pi*c['rpm']/60)
result=dict(note=c['note'],nx=nx,ny=ny,pixels=n,line_rate_hz=fl,line_period_us=1e6/fl,
 active_us=active*1e6,pixel_period_ns=active/nx*1e9,
 required_adc_fs_hz=nx*c['samples_per_pixel']/active,
 feed_mm_s=c['pitch_y_um']*1e-3*fl,scan_s=ny/fl,equivalent_f_mm=f*1000,
 one_channel_16bit_image_bytes=n*2,output_frame_bytes=n*bpp,
 raw_capture_frame_bytes=n*bpp*c['samples_per_pixel'],
 raw_active_bytes_s=c['adc_fs_hz']*bpp,
 average_raw_bytes_s=nx*fl*c['samples_per_pixel']*bpp,
 output_active_bytes_s=nx/active*bpp,average_output_bytes_s=nx*fl*bpp,
 conservative_raw_fifo_for_stall_bytes=math.ceil(c['adc_fs_hz']*bpp*c['fifo_stall_ms']*.001),
 fullrate_125MSPS_twochannel_16bit_bytes_s=125e6*2*2,
 theoretical_gigabit_bytes_s=1e9/8,
 full_frame_pingpong_bytes=2*n*bpp,
 fits_512MiB_with_reserved=2*n*bpp+c['reserved_ddr_bytes']<512*1024**2)
print(json.dumps(result,ensure_ascii=False,indent=2))
(p/'results/production_budget.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
