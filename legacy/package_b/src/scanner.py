"""Transparent behavioral model; NOT a calibrated model of the photographed scanner.
All internal units are SI. See docs/模型边界与验收.md for approximations.
"""
from __future__ import annotations
import csv, json, math, platform
from pathlib import Path
from typing import Any
import numpy as np
from scipy.ndimage import gaussian_filter, map_coordinates
from scipy.signal import lfilter, butter, sosfilt
import tifffile
from PIL import Image

Q = 1.602176634e-19
H = 6.62607015e-34
C = 299792458.0

def load_config(path: str | Path) -> dict[str, Any]:
    with open(path, encoding='utf-8') as f:
        cfg = json.load(f)
    validate(cfg)
    return cfg

def validate(c: dict[str, Any]) -> None:
    s, o, p, a, d = [c[k] for k in ('scan','optics','pmt','analog','adc')]
    s.setdefault('samples_per_pixel',1)
    for k in ('nx', 'ny', 'line_samples', 'oversample', 'samples_per_pixel'):
        if not isinstance(s[k], int) or s[k] <= 0:
            raise ValueError(f'scan.{k} must be a positive integer')
    if s['nx'] < 2:
        raise ValueError('scan.nx must be >= 2 for the spatial coordinate model')
    if not isinstance(s['pre_samples'], int) or s['pre_samples'] < 0:
        raise ValueError('pre_samples must be a nonnegative integer')
    if s['pre_samples'] + s['nx']*s['samples_per_pixel'] + d['latency_samples'] >= s['line_samples']:
        raise ValueError('ROI plus ADC pipeline must fit inside one scan period')
    if s['fs_hz'] <= 0 or min(s['pitch_x_um'],s['pitch_y_um']) <= 0:
        raise ValueError('Sample rate and pitches must be positive')
    if s['y_mode'] not in ('step_hold', 'continuous'):
        raise ValueError('y_mode must be step_hold or continuous')
    a.setdefault('adc_driver_gain',1.0); a.setdefault('adc_driver_offset_V',0.0)
    if a['adc_driver_gain']<=0:
        raise ValueError('adc_driver_gain must be positive')
    if a['mode'] not in ('log', 'linear'):
        raise ValueError('analog mode must be log or linear')
    if not 1 <= d['bits'] <= 16 or d['vmax'] <= d['vmin'] or d['latency_samples'] < 1:
        raise ValueError('ADC: use 1..16 bits, vmax > vmin, latency >= 1')
    if not 0 < p['qe'] <= 1 or p['gain'] <= 0 or p['excess_noise_factor'] < 1:
        raise ValueError('PMT: invalid QE, gain, or excess noise factor')
    if min(o['laser_power_W'], o['pre_film_efficiency'], o['collection_efficiency'],
           o['wavelength_nm'], a['reference_current_A'], a['current_floor_A']) <= 0:
        raise ValueError('Optical and log-reference parameters must be positive')
    if p['dark_anode_A'] < 0 or p['anode_limit_A'] <= 0:
        raise ValueError('PMT: invalid dark current or current limit')
    if s['oversample'] < 2 or s['oversample'] % 2:
        raise ValueError('oversample must be an even integer >= 2; use convergence tests')
    if a['aa_cutoff_hz'] >= s['fs_hz'] * s['oversample'] / 2:
        raise ValueError('Anti-alias cutoff exceeds internal-model Nyquist frequency')

def budget(c: dict[str, Any]) -> dict[str, Any]:
    s,o,p,a,d = [c[k] for k in ('scan','optics','pmt','analog','adc')]
    ts = 1 / s['fs_hz']; line = s['line_samples'] * ts; m=s.get('samples_per_pixel',1); tp=m*ts
    clear_p = o['laser_power_W']*o['pre_film_efficiency']*o['collection_efficiency']
    rate = clear_p * o['wavelength_nm']*1e-9/(H*C) * p['qe']
    nepix = rate * tp
    dmax = c['film']['density_max']
    return dict(
        model_status='ASSUMED BEHAVIORAL MODEL; NOT MEASURED HARDWARE',
        nx=s['nx'], ny=s['ny'], width_mm=s['nx']*s['pitch_x_um']/1000,
        height_mm=s['ny']*s['pitch_y_um']/1000, pixel_period_ns=tp*1e9, adc_period_ns=ts*1e9, adc_samples_per_pixel=m,
        line_period_us=line*1e6, active_time_us=s['nx']*tp*1e6,
        line_rate_hz=1/line, duty=s['nx']*m/s['line_samples'],
        raster_duration_s=s['ny']*line,
        last_valid_word_time_s=(s['ny']-1)*line+(s['pre_samples']+s['nx']*m-.5+d['latency_samples'])*ts,
        scan_speed_m_s=s['pitch_x_um']*1e-6/tp,
        mean_feed_speed_mm_s=s['pitch_y_um']/1000/line,
        raw_frame_bytes=s['nx']*s['ny']*2, raw_adc_roi_bytes=s['nx']*s['ny']*m*2,
        active_burst_bytes_s=s['fs_hz']*2,
        valid_average_bytes_s=s['nx']/line*2, valid_raw_adc_average_bytes_s=s['nx']*m/line*2,
        internal_dt_ns=ts/s['oversample']*1e9,
        clear_detector_power_W=clear_p, clear_signal_anode_A=Q*p['gain']*rate,
        photoelectrons_per_pixel_clear=nepix,
        photoelectrons_per_pixel_at_Dmax=nepix*10**(-dmax),
        approximate_shot_density_sigma_at_Dmax=math.sqrt(p['excess_noise_factor']/(nepix*10**(-dmax)))/math.log(10),
        density_quantization_step=(d['vmax']-d['vmin'])/(2**d['bits']*abs(a['log_slope_V_dec'])*a.get('adc_driver_gain',1.0)) if a['mode']=='log' else None,
        caveat='Shot-noise estimate uses a pixel-length rectangular integration; actual analog noise bandwidth differs.'
    )

def phantom(nx: int, ny: int, dmax: float) -> np.ndarray:
    """Synthetic *density* target, not a photographic negative characteristic curve."""
    x=np.arange(nx)[None,:]; y=np.arange(ny)[:,None]
    v=np.broadcast_to(.12+(dmax-.24)*x/max(1,nx-1), (ny,nx)).copy()
    # Broad step wedge, slanted edge, fine bars and a small high-density inclusion.
    v[:ny//3] = .08 + (dmax-.16)*np.floor(np.minimum(x*10/nx,9))/9
    mid=(y>=ny//3)&(y<2*ny//3)
    v=np.where(mid, np.where(x > nx*.48 + .12*(y-ny/2), .3, dmax-.3), v)
    bars=(y>=2*ny//3)&(x<nx*.55)
    v=np.where(bars, .35+(dmax-.7)*((x//max(2,nx//64))%2), v)
    circle=((x-nx*.78)/(nx*.08))**2+((y-ny*.8)/(ny*.12))**2<1
    return np.clip(np.where(circle,dmax,v),0,dmax)

def static_voltage(current: np.ndarray | float, a: dict[str, Any]) -> np.ndarray:
    cur=np.asarray(current)
    if a['mode']=='log':
        return a['offset_V']+a['log_slope_V_dec']*np.log10(a['reference_current_A']/np.maximum(cur,a['current_floor_A']))
    return a['offset_V']+a['transimpedance_ohm']*cur

def inverse_voltage(v: np.ndarray | float, a: dict[str, Any]) -> np.ndarray:
    v=np.asarray(v)
    if a['mode']=='log':
        return a['reference_current_A']*10**np.clip((a['offset_V']-v)/a['log_slope_V_dec'],-20,20)
    return (v-a['offset_V'])/a['transimpedance_ohm']

def quantize(v: np.ndarray, d: dict[str, Any]) -> np.ndarray:
    """Ideal straight-binary ADC, bins of FSR/2**N, with endpoint saturation."""
    return np.clip(np.floor((v-d['vmin'])/(d['vmax']-d['vmin'])*2**d['bits']),0,2**d['bits']-1).astype(np.uint16)

def code_voltage(codes: np.ndarray, d: dict[str, Any]) -> np.ndarray:
    return d['vmin']+(codes.astype(float)+.5)*(d['vmax']-d['vmin'])/2**d['bits']

def pole(x: np.ndarray, cutoff: float, dt: float, state: float) -> tuple[np.ndarray,float]:
    if cutoff<=0: return x.copy(),float(x[-1])
    alpha=math.exp(-2*math.pi*cutoff*dt)
    out,_=lfilter([1-alpha],[1,-alpha],x,zi=[alpha*state])
    return out,float(out[-1])

def compound_counts(mu: np.ndarray, factor: float, rng: np.random.Generator) -> np.ndarray:
    """Poisson primary electrons with gamma-distributed multiplication.
    E[Q/Gq]=mu; Var[Q/Gq]=F*mu. No Gaussian/negative photon counts.
    """
    n=rng.poisson(mu)
    if factor==1: return n.astype(float)
    out=np.zeros_like(mu,dtype=float); use=n>0
    out[use]=rng.gamma(n[use]/(factor-1),factor-1)
    return out

def reconstruct(codes: np.ndarray, c: dict[str, Any], flat_signal_A: np.ndarray) -> tuple[np.ndarray,np.ndarray]:
    a,p,d=c['analog'],c['pmt'],c['adc']
    v_front=(code_voltage(codes,d)-a.get('adc_driver_offset_V',0.0))/a.get('adc_driver_gain',1.0)
    current=inverse_voltage(v_front,a)
    trans=(current-p['dark_anode_A'])/np.maximum(flat_signal_A,1e-30)
    # Values outside physical range are retained in transmission output for diagnostics.
    den=-np.log10(np.maximum(trans,1e-8))
    return trans,den

def run(c: dict[str, Any], output: Path, density_file: Path | None=None,
        trace_all: bool=False) -> dict[str, Any]:
    validate(c); output.mkdir(parents=True,exist_ok=True)
    s,o,p,a,d=[c[k] for k in ('scan','optics','pmt','analog','adc')]
    nx,ny,nl,pre,osamp=s['nx'],s['ny'],s['line_samples'],s['pre_samples'],s['oversample']
    m=s.get('samples_per_pixel',1); active=nx*m
    fs=s['fs_hz']; dt=1/fs/osamp; ts=1/fs; duration=nl*ts
    rng=np.random.default_rng(c['seed'])
    if density_file:
        den=np.load(density_file) if density_file.suffix.lower()=='.npy' else tifffile.imread(density_file)
        den=np.asarray(den,dtype=float)
        if den.shape!=(ny,nx) or not np.isfinite(den).all() or (den<0).any():
            raise ValueError(f'Density input must be finite, nonnegative, and shape {(ny,nx)}')
    else: den=phantom(nx,ny,c['film']['density_max'])
    truth_T=10**(-den)
    # Convolve TRANSMITTANCE, never density: optical powers sum before the logarithm.
    blur_T=gaussian_filter(truth_T,o['spot_sigma_px'],mode='nearest') if o['spot_sigma_px']>0 else truth_T
    raw=np.empty((ny,active),dtype=np.uint16)
    # Right-endpoint optical quadrature; filters and ADC share this time grid.
    # Even oversampling aligns all ADC centers exactly with internal grid points.
    tc=(np.arange(nl*osamp)+1)*dt; te=tc
    t_adc=(np.arange(nl)+.5)*ts
    x0=(tc*fs-pre)/m-.5
    ud=np.clip(x0/(nx-1),0,1)
    x_deterministic=x0+s['distortion_amplitude_px']*np.sin(2*np.pi*ud)
    xcal_pixel=np.arange(nx)+s['distortion_amplitude_px']*np.sin(2*np.pi*np.arange(nx)/(nx-1))
    xraw=(np.arange(active)+.5)/m-.5
    xcal=xraw+s['distortion_amplitude_px']*np.sin(2*np.pi*np.clip(xraw/(nx-1),0,1))
    flat_cal=np.clip(1-o['collection_vignetting']*(2*np.clip(xcal,0,nx-1)/(nx-1)-1)**2,.05,1)
    power0=o['laser_power_W']*o['pre_film_efficiency']*o['collection_efficiency']
    photon_energy=H*C/(o['wavelength_nm']*1e-9)
    photo_rate0=power0/photon_energy*p['qe']
    signal0=Q*p['gain']*photo_rate0
    flat_signal=signal0*flat_cal
    dark_rate=p['dark_anode_A']/(Q*p['gain'])
    current_state=p['dark_anode_A']
    vdark=float(static_voltage(current_state,a)); log_state=vdark
    if a['aa_cutoff_hz']>0:
        sos=butter(a['aa_order'],a['aa_cutoff_hz'],fs=fs*osamp,output='sos')
        from scipy.signal import sosfilt_zi
        aa_state=sosfilt_zi(sos)*vdark
    else: sos=None; aa_state=None
    first_trace=None; first_micro=None
    stats={'pmt_clip_internal':0,'adc_clip_active':0,'internal_count':ny*nl*osamp,'transmission_nonpositive':0}
    events=[]; all_handle=None
    headers=['adc_sample_time_s','code_ready_time_s','sample_index','phase',
             'x_nominal_um','x_true_um','y_true_um','power_at_detector_W',
             'anode_current_filtered_A','log_or_linear_static_V','adc_input_V',
             'adc_code_sampled','roi_at_sample','roi_at_output_clock','adc_code_at_output_clock','pixel_x_at_output_clock']
    if trace_all:
        all_handle=open(output/'all_adc_trace.csv','w',encoding='utf-8',newline='')
        all_handle.write(','.join(headers)+'\n')
    try:
        for j in range(ny):
            jitter_line=rng.normal(0,s['line_jitter_sigma_px'])
            x=x_deterministic+jitter_line
            y=np.full_like(x,float(j))
            if s['y_mode']=='continuous':
                y += (tc-(pre+active/2)*ts)/duration
            tr=map_coordinates(blur_T,[y,np.clip(x,0,nx-1)],order=1,mode='nearest',prefilter=False)
            # Outside the film area the beam passes through clear space.
            tr=np.where((x>=-.5)&(x<nx-.5),tr,1.0)
            illumination=np.clip(1-o['collection_vignetting']*(2*np.clip(x,0,nx-1)/(nx-1)-1)**2,.05,1)
            laser_gate=(tc>=(pre-o['guard_laser_samples'])*ts)&(tc<(pre+active+o['guard_laser_samples'])*ts)
            drift=1+rng.normal(0,o['line_power_sigma'])
            ripple=1+o['ripple_fraction']*np.sin(2*np.pi*o['ripple_hz']*(tc+j*duration))
            power=power0*tr*illumination*laser_gate*max(drift,0)*ripple
            power=np.maximum(power,0)
            power_delayed=np.interp(tc-o['film_to_pmt_path_m']/C,tc,power,left=0,right=0)
            mu=(power_delayed/photon_energy*p['qe']+dark_rate)*dt
            ne=compound_counts(mu,p['excess_noise_factor'],rng) if p['noise_enabled'] else mu
            current=ne*Q*p['gain']/dt
            current=np.interp(tc-p['transit_time_ns']*1e-9,tc,current,left=p['dark_anode_A'])
            current,current_state=pole(current,p['bandwidth_hz'],dt,current_state)
            # Hard clipping is a behavioral *warning threshold*, not a real PMT space-charge model.
            stats['pmt_clip_internal']+=int(np.count_nonzero(current>p['anode_limit_A']))
            current=np.minimum(current,p['anode_limit_A'])
            v_static=static_voltage(current,a)
            v_log,log_state=pole(v_static,a['log_bandwidth_hz'],dt,log_state)
            if sos is not None:
                v,aa_state=sosfilt(sos,v_log,zi=aa_state)
            else: v=v_log
            sample_times=t_adc+rng.normal(0,d['clock_jitter_ps']*1e-12,nl)
            # ADC aperture is an instantaneous sample, NOT an integration over the whole pixel.
            vs=a['adc_driver_gain']*np.interp(sample_times,te,v,left=vdark)+a['adc_driver_offset_V']
            vs+=rng.normal(0,a['output_noise_rms_V'],nl)
            codes=quantize(vs,d)
            raw[j]=codes[pre:pre+active]
            stats['adc_clip_active']+=int(np.count_nonzero((vs[pre:pre+active]<d['vmin'])|(vs[pre:pre+active]>=d['vmax'])))
            tstart=j*duration
            events.append([j,tstart,tstart+pre*ts,tstart+(pre+.5)*ts,
                           tstart+(pre+active-.5)*ts,tstart+(pre+active-.5+d['latency_samples'])*ts,(j+1)*duration])
            if j==0 or trace_all:
                idx=np.arange(nl); valid=(idx>=pre)&(idx<pre+active); lat=d['latency_samples']
                vout=np.r_[np.zeros(lat,dtype=bool),valid[:-lat]]
                dataout=np.r_[np.full(lat,-1),codes[:-lat].astype(int)]
                pxout=np.where(vout,(idx-lat-pre)//m,-1)
                phase=np.where(idx==0,0,np.where(idx<pre,1,np.where(valid,2,3)))
                rows=np.column_stack([tstart+t_adc,tstart+t_adc+lat*ts,idx,phase,
                    ((t_adc*fs-pre)/m-.5)*s['pitch_x_um'],np.interp(t_adc,tc,x)*s['pitch_x_um'],
                    np.interp(t_adc,tc,y)*s['pitch_y_um'],np.interp(t_adc,tc,power_delayed),
                    np.interp(t_adc,te,current),np.interp(t_adc,te,v_static),vs,codes,valid,vout,dataout,pxout])
                if j==0:
                    first_trace=rows
                    keep=(te>=(pre-8)*ts)&(te<(pre+24)*ts)
                    first_micro=np.column_stack([te[keep],x[keep]*s['pitch_x_um'],power_delayed[keep],current[keep],v_static[keep],v[keep]])
                if all_handle:
                    np.savetxt(all_handle,rows,delimiter=',',fmt='%.10g')
    finally:
        if all_handle: all_handle.close()
    trans_samples,_=reconstruct(raw,c,flat_signal[None,:])
    # Linearize the log channel and dark/flat correct BEFORE spatial binning.
    # Averaging logarithmic ADC codes would estimate a different quantity.
    trans=trans_samples.reshape(ny,nx,m).mean(axis=2)
    drec=-np.log10(np.maximum(trans,1e-8))
    # Static geometry LUT regridding; does not know/correct random line jitter or feed shear.
    dgeom=np.array([np.interp(np.arange(nx),xcal_pixel,row) for row in drec])
    target=-np.log10(np.maximum(blur_T,1e-12))
    metrics=budget(c)
    metrics.update(stats)
    metrics.update(density_rmse_unblurred=float(np.sqrt(np.mean((drec-den)**2))),
                   density_rmse_spatially_blurred=float(np.sqrt(np.mean((drec-target)**2))),
                   density_rmse_after_static_geometry=float(np.sqrt(np.mean((dgeom-target)**2))),
                   density_bias=float(np.mean(drec-target)),
                   transmission_nonpositive=int(np.count_nonzero(trans<=0)),
                   calibration='Model-known STATIC dark/flat; inverse log -> linear transmission -> pixel binning. No measured calibration or deconvolution.',
                   model_limitations=['constant analog poles','hard PMT saturation threshold','no coherence/speckle',
                       'no real motor servo','no real ADC INL/DNL or intrinsic ADC input noise','no analog delay deconvolution',
                       'no DDR or DMA emulation','not a color-recovery model'])
    raw.astype('<u2').tofile(output/'raw_roi_u16le.bin')
    for name,arr in [('raw_codes_u16.tif',raw),('density_truth_f32.tif',den.astype('float32')),
                     ('density_reconstructed_f32.tif',drec.astype('float32')),
                     ('density_geometry_corrected_f32.tif',dgeom.astype('float32')),
                     ('transmittance_f32.tif',trans.astype('float32'))]:
        tifffile.imwrite(output/name,arr,photometric='minisblack',metadata={'axes':'YX','model':'ASSUMED_SIMULATION'})
    scale=max(c['film']['density_max'],1e-6)
    Image.fromarray(np.uint8(np.clip(drec/scale,0,1)*255)).save(output/'preview_density.png')
    Image.fromarray(np.uint8(np.clip(den/scale,0,1)*255)).save(output/'preview_truth.png')
    Image.fromarray(np.uint8(np.clip(trans,0,1)*255)).save(output/'preview_transmission.png')
    tifffile.imwrite(output/'density_display_u16.tif',np.uint16(np.clip(drec/scale,0,1)*65535),metadata={'density_scale_per_code':scale/65535})
    np.savetxt(output/'line_trace.csv',first_trace,delimiter=',',header=','.join(headers),comments='',fmt='%.10g')
    np.savetxt(output/'micro_trace.csv',first_micro,delimiter=',',header='time_s,x_um,power_W,current_A,static_V,adc_input_V',comments='',fmt='%.10g')
    np.savetxt(output/'line_events.csv',events,delimiter=',',header='line_id,sos_s,roi_start_s,first_sample_s,last_sample_s,last_word_ready_s,next_sos_s',comments='',fmt='%.12g')
    np.savez_compressed(output/'simulation_arrays.npz',raw_codes=raw,density=den,reconstructed=drec,transmittance=trans,flat_signal_A=flat_signal)
    for name,obj in [('metrics.json',metrics),('config_used.json',c),('calibration_model.json',{
        'status':'MODEL-KNOWN IDEAL STATIC CALIBRATION, not measured',
        'dark_anode_A':p['dark_anode_A'],'flat_signal_A':flat_signal.tolist(),
        'voltage_inverse':a,'ADC':d}),('environment.json',{'python':platform.python_version(),'numpy':np.__version__})]:
        (output/name).write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8')
    # One actual ADC line for RTL testbench/data import, not fabricated test results.
    with open(output/'adc_line.hex','w',encoding='ascii') as f:
        for code in first_trace[:,11].astype(int): f.write(f'{code:04x}\n')
    from .viewer import write_viewer
    write_viewer(output,c,metrics,first_trace)
    return metrics
