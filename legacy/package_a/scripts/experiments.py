#!/usr/bin/env python3
"""Small ablation experiments. Run: python scripts/experiments.py
No experimental results are represented as measured hardware performance.
"""
import sys, json, copy, csv
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from src.scanner_sim import load_config,run
base=load_config(ROOT/'config/demo.json');base.update(nx=128,ny=48,trace_lines=0)
cases=[('baseline',{}),('no_shot_noise',dict(shot_noise=False)),('wide_log_bandwidth',dict(log_bw_hz=8e6)),('slow_log_bandwidth',dict(log_bw_hz=.5e6)),('larger_spot',dict(spot_fwhm_um=100.)),('more_stray_light',dict(stray_fraction=.002)),('current_dependent_log_bw',dict(log_bw_current_dependent=True))]
rows=[]
for name,patch in cases:
 c=copy.deepcopy(base);c.update(patch);r=run(c)['metrics'];row=dict(case=name,rmse=r['density_rmse_valid_interior'],mae=r['density_mae_valid_interior'],invalid_fraction=r['invalid_fraction'],adc_clips=r['adc_clipped_samples']);rows.append(row);print(row)
out=ROOT/'results/ablation.csv'
with out.open('w',newline='',encoding='utf-8-sig') as f:
 w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
