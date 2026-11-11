#!/usr/bin/env python3
"""Deterministic timestep refinement; stochastic realizations should not be compared pixelwise."""
import copy,json,sys,tempfile,csv
from pathlib import Path
import numpy as np
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from src.scanner import load_config,run
c=load_config(ROOT/'configs/demo_log.json');c['scan'].update(nx=64,ny=16,pre_samples=64,line_samples=512,line_jitter_sigma_px=0)
c['optics'].update(line_power_sigma=0,ripple_fraction=0);c['pmt']['noise_enabled']=False
c['analog']['output_noise_rms_V']=0;c['adc']['clock_jitter_ps']=0
images={};rows=[]
with tempfile.TemporaryDirectory() as t:
 for os in [4,8,16,32]:
  cc=copy.deepcopy(c);cc['scan']['oversample']=os;out=Path(t)/str(os);run(cc,out)
  images[os]=np.load(out/'simulation_arrays.npz')['reconstructed']
 for os in [4,8,16]:
  delta=images[os]-images[32];rows.append([os,1/c['scan']['fs_hz']/os*1e9,float(np.sqrt(np.mean(delta**2))),float(abs(delta).max())])
out=ROOT/'examples/convergence.csv'
with out.open('w',newline='') as f:
 w=csv.writer(f);w.writerow(['oversample','dt_ns','RMSE_D_vs_32x','max_abs_D_vs_32x']);w.writerows(rows)
print(out.read_text())
