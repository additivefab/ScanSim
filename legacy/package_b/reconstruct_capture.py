#!/usr/bin/env python3
"""Reconstruct an already packet-validated, ROI-only, row-major ADC binary file.
Does not decode Ethernet packets or guess image sizes. ADC words must be unsigned
little-endian 16-bit containers, regardless of the ADC's configured coding.
"""
import argparse,json,sys
from pathlib import Path
import numpy as np
import tifffile
from src.scanner import load_config,reconstruct
p=argparse.ArgumentParser();p.add_argument('--raw',required=True);p.add_argument('--config',required=True)
p.add_argument('--calibration',required=True);p.add_argument('--output',default='reconstructed')
p.add_argument('--coding',choices=['offset-binary','twos-complement'],required=True)
p.add_argument('--allow-model-calibration',action='store_true',help='Only for synthetic examples, never for hardware qualification')
a=p.parse_args()
try:
 c=load_config(a.config);cal=json.loads(Path(a.calibration).read_text(encoding='utf-8'))
 if 'MODEL' in cal.get('status','').upper() and not a.allow_model_calibration:
  raise ValueError('This calibration is synthetic; use a measured calibration or explicitly allow model calibration for a demo')
 s=c['scan'];n=s['nx']*s['ny']*s['samples_per_pixel'];raw=np.fromfile(a.raw,dtype='<u2')
 if raw.size!=n: raise ValueError(f'Expected exactly {n} ROI samples, found {raw.size}; do not silently drop/pad samples')
 bits=c['adc']['bits']
 if np.any(raw>=2**bits): raise ValueError('ADC words exceed the configured bit width')
 if a.coding=='twos-complement': raw=np.bitwise_xor(raw,np.uint16(1<<(bits-1)))
 c['analog']=cal['voltage_inverse'];c['adc']=cal['ADC'];c['pmt']['dark_anode_A']=float(cal['dark_anode_A'])
 flat=np.asarray(cal['flat_signal_A'],dtype=float)
 if flat.shape!=(s['nx']*s['samples_per_pixel'],) or not np.isfinite(flat).all() or (flat<=0).any():
  raise ValueError('flat_signal_A must have nx*samples_per_pixel finite positive entries')
 raw=raw.reshape(s['ny'],-1);tr,_=reconstruct(raw,c,flat[None,:]);tr=tr.reshape(s['ny'],s['nx'],-1).mean(axis=2)
 den=-np.log10(np.maximum(tr,1e-8));out=Path(a.output);out.mkdir(parents=True,exist_ok=True)
 tifffile.imwrite(out/'density_f32.tif',den.astype('float32'));tifffile.imwrite(out/'transmittance_f32.tif',tr.astype('float32'))
 (out/'quality.json').write_text(json.dumps({'nonpositive_transmission_pixels':int((tr<=0).sum()),
 'rail_code_samples':int(((raw==0)|(raw==2**bits-1)).sum()),'calibration_status':cal.get('status','UNSPECIFIED'),
 'note':'No analog deconvolution, geometric resampling, or color/film-tone inversion was performed.'},indent=2),encoding='utf-8')
 print(out.resolve())
except (ValueError,OSError,KeyError) as exc: p.error(str(exc))
