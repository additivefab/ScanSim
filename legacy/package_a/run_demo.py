#!/usr/bin/env python3
"""Run from the package root: python run_demo.py --config config/demo.json."""
from __future__ import annotations
import argparse, csv, json, platform, sys
from pathlib import Path
import numpy as np
from PIL import Image
import scipy
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from src.scanner_sim import load_config, run, write_trace, timing

ROOT=Path(__file__).resolve().parent

def save_outputs(result:dict,out:Path) -> None:
    out.mkdir(parents=True,exist_ok=True);c=result['config'];ti=timing(c)
    (out/'config_used.json').write_text(json.dumps(c,ensure_ascii=False,indent=2),encoding='utf-8')
    result['metrics']['environment']=dict(python=platform.python_version(),numpy=np.__version__,scipy=scipy.__version__,matplotlib=matplotlib.__version__)
    (out/'metrics.json').write_text(json.dumps(result['metrics'],ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    np.savez_compressed(out/'simulation_arrays.npz',**{k:result[k] for k in ['truth_density','blurred_transmission','density','transmission','invalid','optical_reference_density','white_current','opaque_current']})
    # Displayed optical density is mapped linearly. This is NOT a calibrated DICOM image.
    preview=np.uint8(np.rint(np.clip(result['density']/c['density_max'],0,1)*255))
    Image.fromarray(preview).save(out/'density_preview.png')
    Image.fromarray(np.uint8(np.rint(np.clip(result['optical_reference_density']/c['density_max'],0,1)*255))).save(out/'optical_reference_preview.png')
    Image.fromarray(np.uint8(result['invalid'])*255).save(out/'invalid_mask.png')
    Image.fromarray(np.uint8(np.rint(np.clip(result['truth_density']/c['density_max'],0,1)*255))).save(out/'phantom_density.png')
    Image.fromarray(result['density'].astype('float32')).save(out/'density_float32.tiff')
    # Preserve sample values separately from calibrated density. File format is explicit.
    raw=np.concatenate(result['active_codes']).astype('<u2')
    raw.tofile(out/'adc_active_u16le.raw')
    (out/'raw_format.json').write_text(json.dumps(dict(dtype='uint16',byte_order='little',layout='row-major; line then ADC sample; one channel',shape=[c['ny'],c['nx']*c['samples_per_pixel']],notes='ADC log-voltage codes, NOT density. Guard-window traces are in first_lines_trace.csv.'),indent=2),encoding='utf-8')
    write_trace(out/'first_lines_trace.csv',result['trace'])
    with (out/'frame_events.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f);w.writerow(['time_s','event','line','facet','meaning'])
        w.writerow([0,'FRAME_START',0,0,'Reference time after hypothetical warmup/lock/calibration; durations are NOT measured'])
        events=[]
        for j in range(c['ny']):
            base=j*ti['line_period_s'];a=c['active_start_us']*1e-6
            for t,name,note in [(base,'SOS', 'Hypothetical line start; not observed from uploaded oscilloscope screenshot'),
                (base+a-2e-6,'LASER_GATE_ON','Assumed blanking policy'),
                (base+a,'ACTIVE_OPTICAL_START','Film crop begins'),
                (base+a+ti['active_s'],'ACTIVE_OPTICAL_END','Film crop ends'),
                (base+a+ti['active_s']+2e-6,'LASER_GATE_OFF','Assumed blanking policy'),
                (base+a+ti['active_s']+ti['nominal_analog_delay_s']+ti['adc_pipeline_s']+c['adc_tpd_ns']*1e-9,'LAST_DATA_NOMINAL','Estimate; exact tags appear in sampled trace')]:
                events.append((t,name,j,j%c['facets'],note))
        for row in sorted(events):w.writerow(row)
        w.writerow([ti['frame_scan_s'],'FRAME_COMPLETE',c['ny']-1,(c['ny']-1)%c['facets'],'Only after sample count and integrity checks; DMA time not simulated'])
    with (out/'density_patches.csv').open('w',encoding='utf-8-sig',newline='') as f:
        rows=result['metrics']['density_patches'];w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    # Distinct figures, default plotting colours; no fabricated measured plots.
    tr=result['trace'][0]
    t=(tr['sample_time_s']-c['active_start_us']*1e-6)*1e6
    fig=plt.figure(figsize=(9,3.6));plt.plot(t,tr['v_adc_diff_v'],label='ADC input (differential)');plt.xlabel('Time from optical crop start [us]');plt.ylabel('Voltage [V]');plt.title('Synthetic first-line analogue waveform');plt.legend();plt.tight_layout();fig.savefig(out/'analogue_waveform.png',dpi=160);plt.close(fig)
    fig=plt.figure(figsize=(9,3.6));plt.plot(t,tr['adc_sample_code'],label='Code for current sample');plt.plot(t,tr['adc_bus_code'],label='Code on bus after pipeline');plt.xlim(-.3,3);plt.xlabel('Time from optical crop start [us]');plt.ylabel('ADC code');plt.title('12-cycle pipeline demonstration (CMOS behavioural mode)');plt.legend();plt.tight_layout();fig.savefig(out/'pipeline_waveform.png',dpi=160);plt.close(fig)
    row=max(1,int(c['ny']*.15));fig=plt.figure(figsize=(9,3.6));plt.plot(result['optical_reference_density'][row],label='Optical reference');plt.plot(result['density'][row],label='Reconstructed');plt.xlabel('Column');plt.ylabel('Optical density');plt.title('Synthetic density wedge; noise and transient errors retained');plt.legend();plt.tight_layout();fig.savefig(out/'density_profile.png',dpi=160);plt.close(fig)
    vec=np.concatenate(result['active_codes'])[:256]
    (out/'rtl_vectors.hex').write_text(''.join(f'{int(v):04x}\n' for v in vec),encoding='ascii')
    print(json.dumps(result['metrics'],ensure_ascii=False,indent=2))


def main() -> int:
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',type=Path,default=ROOT/'config/demo.json');p.add_argument('--out',type=Path,default=ROOT/'results/default')
    a=p.parse_args()
    try:
        result=run(load_config(a.config));save_outputs(result,a.out)
        from scripts.make_viewer import make_viewer
        make_viewer(result,a.out/'viewer.html')
        return 0
    except (OSError,ValueError,RuntimeError) as exc:
        print(f'ERROR: {exc}',file=sys.stderr);return 1

if __name__=='__main__':raise SystemExit(main())
