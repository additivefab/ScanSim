#!/usr/bin/env python3
"""First-order optical/rotor consistency calculator; not a lens prescription."""
import argparse,json,math
from pathlib import Path
import numpy as np
p=argparse.ArgumentParser();p.add_argument('--f-mm',type=float,default=200);p.add_argument('--facets',type=int,default=6)
p.add_argument('--rpm',type=float,default=10000);p.add_argument('--pitch-um',type=float,default=50)
p.add_argument('--theta-max-rad',type=float,default=.5);p.add_argument('--out',default='optics/geometry.csv')
a=p.parse_args()
if min(a.f_mm,a.facets,a.rpm,a.pitch_um,a.theta_max_rad)<=0 or a.theta_max_rad>=math.pi/2:
 p.error('Use positive inputs and optical angle < pi/2')
theta=np.linspace(-a.theta_max_rad,a.theta_max_rad,401);f=a.f_mm
out=Path(a.out);out.parent.mkdir(parents=True,exist_ok=True)
np.savetxt(out,np.column_stack([theta,theta/2,f*theta,f*np.tan(theta)]),delimiter=',',
 header='optical_angle_rad,mirror_mechanical_delta_rad,ideal_ftheta_x_mm,ordinary_lens_x_mm',comments='')
w=a.rpm*2*math.pi/60;v=2*f/1000*w;line=a.facets*a.rpm/60
print(json.dumps({'status':'IDEAL GEOMETRY ONLY; NOT PHOTO-DERIVED LENS PARAMETERS',
 'line_rate_hz':line,'scan_speed_m_s':v,'pixel_rate_for_requested_pitch_hz':v/(a.pitch_um*1e-6),
 'active_width_mm':2*f*a.theta_max_rad,'active_duty_ideal':a.facets*a.theta_max_rad/(2*math.pi),
 'f_mm_needed_for_10MHz_pixels_at_requested_pitch':(a.pitch_um*1e-6*1e7)/(2*w)*1000},indent=2))
