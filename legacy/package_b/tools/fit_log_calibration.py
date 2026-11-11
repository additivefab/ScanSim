#!/usr/bin/env python3
"""Fit measured steady-state voltage = offset + slope*log10(Iref/current)."""
import argparse,csv,json
from pathlib import Path
import numpy as np
p=argparse.ArgumentParser();p.add_argument('csv_file');p.add_argument('--iref',type=float,default=40e-6);p.add_argument('--out',default='log_fit.json');a=p.parse_args()
try:
 with open(a.csv_file,encoding='utf-8-sig',newline='') as f: rows=list(csv.DictReader(f))
 current=np.array([float(r['current_A']) for r in rows]);voltage=np.array([float(r['voltage_V']) for r in rows])
 if len(rows)<3 or a.iref<=0 or np.any(current<=0) or not np.isfinite(current).all() or not np.isfinite(voltage).all():
  raise ValueError('Need at least 3 finite data pairs and strictly positive currents/Iref')
 x=np.log10(a.iref/current)
 if np.ptp(x)<.5: raise ValueError('Calibration current span must cover at least 0.5 decade')
 slope,offset=np.polyfit(x,voltage,1);res=voltage-(slope*x+offset)
 obj={'input_file':str(a.csv_file),'reference_current_A':a.iref,'log_slope_V_dec':float(slope),
 'offset_V':float(offset),'rmse_V':float(np.sqrt(np.mean(res**2))),'max_abs_residual_V':float(abs(res).max()),
 'note':'Static fit only. Input current includes dark/bias contributions as actually applied. Not a bandwidth calibration.'}
 Path(a.out).write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(obj,indent=2))
except (OSError,ValueError,KeyError) as exc: p.error(str(exc))
