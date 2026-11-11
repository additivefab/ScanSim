#!/usr/bin/env python3
"""Deterministic time-step convergence report. No automatic hardware-accuracy claim."""
from pathlib import Path
import sys,csv,copy
import numpy as np
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from src.scanner_sim import load_config,run
c=load_config(ROOT/'config/demo.json');c.update(nx=128,ny=32,trace_lines=0,shot_noise=False,adc_noise_lsb=0.,electronic_noise_v=0.)
rows=[];previous=None
for osr in [4,8,16,32]:
 cc=copy.deepcopy(c);cc['analog_oversample']=osr;r=run(cc);d=r['density']
 row=dict(oversample=osr,dt_ns=1e9/(cc['adc_fs_hz']*osr),density_rmse=r['metrics']['density_rmse_valid_interior'],difference_rmse_from_previous=None if previous is None else float(np.sqrt(np.mean((d-previous)**2))))
 rows.append(row);previous=d;print(row)
with (ROOT/'results/convergence.csv').open('w',encoding='utf-8-sig',newline='') as f:
 w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
