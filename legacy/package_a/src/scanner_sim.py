"""Transmission-film scanner behavioural reference model.
Not a reverse-engineered hardware model; see docs for assumptions and omissions.
SI internally. Optical convolution is performed on TRANSMITTANCE, never density.
The stochastic PMT uses compound Poisson/gamma gain, not Gaussian light at low counts.
"""
from __future__ import annotations
import csv, json, math
from pathlib import Path
from typing import Any
import numpy as np
from scipy.ndimage import gaussian_filter, map_coordinates
from scipy.signal import lfilter

H = 6.62607015e-34
C = 299792458.0
E = 1.602176634e-19


def load_config(path: str | Path) -> dict[str, Any]:
    c = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_config(c)
    return c


def validate_config(c: dict[str, Any]) -> None:
    for k in ("nx", "ny", "samples_per_pixel", "analog_oversample", "facets", "phantom_supersample"):
        if not isinstance(c[k], int) or c[k] < 1:
            raise ValueError(f"{k} must be a positive integer")
    for k in ("pitch_x_um", "pitch_y_um", "adc_fs_hz", "rpm", "wavelength_nm", "pmt_gain", "log_slope_v_dec"):
        if c[k] <= 0:
            raise ValueError(f"{k} must be positive")
    for k in ("qe", "eta_pre", "eta_collect", "attenuation"):
        if not 0 < c[k] <= 1:
            raise ValueError(f"{k} must be in (0, 1]")
    if not 1 <= c["adc_bits"] <= 16 or c["adc_vmin"] >= c["adc_vmax"]:
        raise ValueError("ADC configuration invalid")
    if c["adc_latency_cycles"] < 1 or c["pmt_excess_noise_factor"] < 1:
        raise ValueError("latency >= 1 and PMT excess noise factor >= 1 required")
    for key in ("facet_gain", "facet_offset_px"):
        if len(c[key]) != c["facets"]:
            raise ValueError(f"{key} must contain facets entries")
    if c["active_start_us"] <= c["guard_us"]:
        raise ValueError("active_start_us must exceed guard_us")
    if (c["active_start_us"] + 2*c["guard_us"])*1e-6 + c["nx"]*c["samples_per_pixel"]/c["adc_fs_hz"] >= 60/(c["facets"]*c["rpm"]):
        raise ValueError("Acquisition window does not fit in line period")
    # AD9268 rates below this need explicit reconsideration; not silently accepted.
    if not 20e6 <= c["adc_fs_hz"] <= 125e6:
        raise ValueError("Example assumes AD9268 DCS-enabled 20..125 MSPS; revise model for another mode")


def timing(c: dict[str, Any]) -> dict[str, float]:
    fs = c["adc_fs_hz"]
    fl = c["facets"]*c["rpm"]/60
    active = c["nx"]*c["samples_per_pixel"]/fs
    vx = c["pitch_x_um"]*1e-6*fs/c["samples_per_pixel"]
    omega = 2*np.pi*c["rpm"]/60
    # Small-signal low-frequency group delay. NOT a deconvolution of edge tails.
    tau_lp = sum(1/(2*np.pi*b) for b in (c["pmt_bw_hz"], c["log_bw_hz"], c["aa_bw_hz"], c["aa_bw_hz"]) if b > 0)
    pure = c["optical_path_m"]/C + c["pmt_transit_ns"]*1e-9
    return dict(line_rate_hz=fl, line_period_s=1/fl, active_s=active,
                pixel_period_s=c["samples_per_pixel"]/fs, adc_period_s=1/fs,
                dt_s=1/(fs*c["analog_oversample"]), vx_m_s=vx,
                vy_m_s=c["pitch_y_um"]*1e-6*fl, equivalent_f_m=vx/(2*omega),
                pure_delay_s=pure, nominal_analog_delay_s=pure+tau_lp,
                adc_pipeline_s=c["adc_latency_cycles"]/fs,
                frame_scan_s=c["ny"]/fl)


def make_phantom(c: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    """Synthetic OD target: step wedges, slope, lines, slanted edge, circular feature."""
    s = c["phantom_supersample"]
    ny, nx = c["ny"]*s, c["nx"]*s
    yy, xx = np.mgrid[0:ny, 0:nx]
    x, y = (xx+.5)/nx, (yy+.5)/ny
    d = np.full((ny,nx), .35)
    # Eleven finite-density patches, no fictitious measured calibration target.
    band = y < .27
    d[band] = (np.floor(np.minimum(x[band], .999999)*11)/10)*c["density_max"]
    band = (y >= .30) & (y < .48)
    d[band] = c["density_max"]*x[band]
    band = (y >= .52) & (y < .74) & (x < .65)
    stripes = np.sin(2*np.pi*28*x)
    d[band] = np.where(stripes[band] > 0, 2.1, .25)
    band = (y >= .78) & (x < .65)
    d[band] = np.where(x[band] > .24+.18*(y[band]-.78), 2.6, .2)
    disk = ((x-.81)/.13)**2+((y-.68)/.22)**2 < 1
    d[disk] = 1.6
    hole = ((x-.81)/.045)**2+((y-.68)/.08)**2 < 1
    d[hole] = .3
    trans = 10.0**(-d)
    return d, trans


def optical_blur(trans: np.ndarray, c: dict[str, Any]) -> np.ndarray:
    s = c["phantom_supersample"]
    sigma = c["spot_fwhm_um"]/2.354820045
    if sigma == 0:
        return trans.copy()
    return gaussian_filter(trans, sigma=(sigma/c["pitch_y_um"]*s, sigma/c["pitch_x_um"]*s), mode="nearest")


def lowpass(x: np.ndarray, bandwidth: float, dt: float, initial: float) -> np.ndarray:
    """Exact first-order integration for a piecewise-LINEAR input (FOH).
    Avoids the half-substep phase advance of naive current-sample ZOH filtering.
    The finite time grid still requires a convergence test.
    """
    if bandwidth <= 0:
        return x.copy()
    u = 2*math.pi*bandwidth*dt
    a = math.exp(-u)
    r = -math.expm1(-u)/u
    b_current, b_previous = 1-r, r-a
    return lfilter([b_current,b_previous], [1,-a], x, zi=[(1-b_current)*initial])[0]


def variable_log_lowpass(x: np.ndarray, current: np.ndarray, c: dict[str, Any], dt: float, initial: float) -> np.ndarray:
    """Illustrative current-dependent bandwidth; NOT an AD8304/actual-board macro-model."""
    bw = np.maximum(c["log_bw_floor_hz"], c["log_bw_hz"]*np.minimum(1., current/c["log_bw_knee_a"]))
    u = 2*np.pi*bw*dt
    a = np.exp(-u)
    ratio = -np.expm1(-u)/u
    out = np.empty_like(x)
    value = initial
    previous = initial
    for k in range(len(x)):
        value = a[k]*value + (ratio[k]-a[k])*previous + (1-ratio[k])*x[k]
        previous = x[k]
        out[k] = value
    return out


def log_voltage(i: np.ndarray | float, c: dict[str, Any]) -> np.ndarray:
    return c["log_offset_v"]+c["log_slope_v_dec"]*np.log10(np.maximum(i,c["log_i_floor_a"])/c["log_i_ref_a"])


def inverse_log(v: np.ndarray, c: dict[str, Any]) -> np.ndarray:
    return c["log_i_ref_a"]*10.0**((v-c["log_offset_v"])/c["log_slope_v_dec"])


def quantize(v: np.ndarray, c: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    lsb = (c["adc_vmax"]-c["adc_vmin"])/(2**c["adc_bits"])
    clipped = (v < c["adc_vmin"]) | (v >= c["adc_vmax"])
    codes = np.clip(np.floor((v-c["adc_vmin"])/lsb),0,2**c["adc_bits"]-1).astype(np.uint16)
    return codes, clipped


def code_to_v(codes: np.ndarray, c: dict[str, Any]) -> np.ndarray:
    lsb = (c["adc_vmax"]-c["adc_vmin"])/(2**c["adc_bits"])
    return c["adc_vmin"]+(codes.astype(float)+.5)*lsb


def delayed(arr: np.ndarray, n: int, fill: float | int = 0) -> np.ndarray:
    out = np.full_like(arr, fill)
    if n < len(arr):
        out[n:] = arr[:-n]
    return out


def beam_xy(t: np.ndarray, line: int, c: dict[str, Any], ti: dict[str, float]) -> tuple[np.ndarray,np.ndarray]:
    f = line % c["facets"]
    x = (t-c["active_start_us"]*1e-6)*c["adc_fs_hz"]/c["samples_per_pixel"]-.5
    u = (x+.5)/c["nx"]*2-1
    x = x+c["facet_offset_px"][f]+c["distortion_px"]*u**3
    y = np.full_like(x,float(line))
    if c["continuous_feed"]:
        y += (t-c["active_start_us"]*1e-6-ti["active_s"]/2)/ti["line_period_s"]
    return x,y


def sample_scene(blurred: np.ndarray, x: np.ndarray, y: np.ndarray, c: dict[str, Any]) -> np.ndarray:
    s = c["phantom_supersample"]
    # x=0 is the centre of output pixel 0. Fine-grid pixel-centre conversion.
    return map_coordinates(blurred, [(y+.5)*s-.5,(x+.5)*s-.5],order=1,mode="nearest",prefilter=False)


def simulate_line(line: int, blurred: np.ndarray, c: dict[str, Any], rng: np.random.Generator,
                  calibration: str | None = None, keep_trace: bool = False) -> dict[str, Any]:
    ti = timing(c); fs = c["adc_fs_hz"]; osr = c["analog_oversample"]; dt=ti["dt_s"]
    # Continuous analogue integration is limited to an acquisition window plus settling guards.
    start_sample = math.floor((c["active_start_us"]-c["guard_us"])*1e-6*fs)
    end_sample = math.ceil((c["active_start_us"]+c["guard_us"])*1e-6*fs+c["nx"]*c["samples_per_pixel"])
    sample_idx = np.arange(start_sample,end_sample,dtype=int)
    th = (np.arange(start_sample*osr,end_sample*osr)+.5)*dt
    ta = (sample_idx+.5)/fs
    x,y = beam_xy(th-ti["pure_delay_s"],line,c,ti)
    gate = (th-ti["pure_delay_s"] >= c["active_start_us"]*1e-6-2e-6)&(th-ti["pure_delay_s"] < c["active_start_us"]*1e-6+ti["active_s"]+2e-6)
    if calibration == "white":
        tr = np.ones_like(th)
    elif calibration == "opaque":
        tr = np.zeros_like(th)
    else:
        tr = sample_scene(blurred,x,y,c)
    frac=(x+.5)/c["nx"]*2-1
    eta=np.maximum(.01,1-c["collection_rolloff"]*frac**2)*c["facet_gain"][line%c["facets"]]
    clear_power=c["laser_power_w"]*c["eta_pre"]*c["eta_collect"]*c["attenuation"]
    p=clear_power*eta*gate*(tr+c["stray_fraction"])
    e_photon=H*C/(c["wavelength_nm"]*1e-9)
    mean_e=p*dt/e_photon*c["qe"] + c["dark_anode_current_a"]*dt/(E*c["pmt_gain"])
    noiseless=calibration is not None or not c["shot_noise"]
    if noiseless:
        i=mean_e*E*c["pmt_gain"]/dt
    else:
        count=rng.poisson(mean_e)
        fex=c["pmt_excess_noise_factor"]
        if fex > 1:
            multiplied=np.zeros_like(mean_e)
            nonzero=count>0
            multiplied[nonzero]=rng.gamma(count[nonzero]/(fex-1),c["pmt_gain"]*(fex-1))
        else:
            multiplied=count*c["pmt_gain"]
        i=multiplied*E/dt
    i=lowpass(i,c["pmt_bw_hz"],dt,c["dark_anode_current_a"])
    pmt_clipped=i > c["pmt_clip_a"]
    i=np.minimum(i,c["pmt_clip_a"])
    vstatic=log_voltage(i,c)
    vdark=float(log_voltage(c["dark_anode_current_a"],c))
    if c["log_bw_current_dependent"]:
        vlog=variable_log_lowpass(vstatic,i,c,dt,vdark)
    else:
        vlog=lowpass(vstatic,c["log_bw_hz"],dt,vdark)
    vaa=lowpass(lowpass(vlog,c["aa_bw_hz"],dt,vdark),c["aa_bw_hz"],dt,vdark)
    jitter = rng.normal(0,c["sample_jitter_ps"]*1e-12,len(ta)) if calibration is None else 0.
    sampled=np.interp(ta+jitter,th,vaa)
    if calibration is None:
        lsb=(c["adc_vmax"]-c["adc_vmin"])/(2**c["adc_bits"])
        sampled += rng.normal(0,math.hypot(c["electronic_noise_v"],c["adc_noise_lsb"]*lsb),len(ta))
    codes,saturated=quantize(sampled,c)
    # Inverse-log per ADC sample BEFORE pixel averaging. Log-mean != linear-mean.
    current=inverse_log(code_to_v(codes,c),c)
    xe,ye=beam_xy(ta-ti["nominal_analog_delay_s"],line,c,ti)
    target=(np.arange(c["nx"]*c["samples_per_pixel"])+.5)/c["samples_per_pixel"]-.5
    recovered=np.interp(target,xe,current).reshape(c["nx"],c["samples_per_pixel"]).mean(axis=1)
    px_sat=(np.interp(target,xe,saturated.astype(float)).reshape(c["nx"],c["samples_per_pixel"]).max(axis=1)>.01)
    # Gate is expressed at sampling time; pipeline moves all tags together.
    gate_start=round((c["active_start_us"]*1e-6+ti["nominal_analog_delay_s"])*fs-.5)
    nominal_index=sample_idx-gate_start
    valid=(nominal_index>=0)&(nominal_index<c["nx"]*c["samples_per_pixel"])
    result=dict(current=recovered,saturated=px_sat,
                pmt_clip_count=int(pmt_clipped.sum()),adc_clip_count=int(saturated.sum()),
                active_codes=codes[valid],first_active_sample=int(gate_start))
    if keep_trace:
        nlat=c["adc_latency_cycles"]
        output_valid=delayed(valid,nlat,False)
        tags=delayed(nominal_index,nlat,-1)
        # Raw bus trace: timing after the named sampling edge; propagation delay is a separate column.
        cols=dict(line=np.full(len(ta),line),adc_cycle=sample_idx,
            sample_time_s=line*ti["line_period_s"]+ta,
            bus_settle_time_s=line*ti["line_period_s"]+ta+c["adc_tpd_ns"]*1e-9,
            source_sample_time_s=line*ti["line_period_s"]+ta-nlat/fs,
            x_sample_equiv_px=xe,y_sample_equiv_px=ye,
            p_detector_w=np.interp(ta,th,p),pmt_anode_a=np.interp(ta,th,i),
            v_log_v=np.interp(ta,th,vlog),v_adc_diff_v=sampled,
            adc_sample_code=codes,adc_bus_code=delayed(codes,nlat,0),
            sample_valid=valid.astype(int),bus_valid=output_valid.astype(int),
            bus_pixel_col=np.where(output_valid,tags//c["samples_per_pixel"],-1),
            adc_saturated=saturated.astype(int))
        result["trace"]=cols
    return result


def rectify_feed(image: np.ndarray,c: dict[str,Any]) -> np.ndarray:
    if not c["continuous_feed"]:
        return image.copy()
    ti=timing(c)
    # First-order trajectory rectification; facet residual and distortions are limitations.
    offsets=((np.arange(c["nx"])+.5)*c["samples_per_pixel"]/c["adc_fs_hz"]-ti["active_s"]/2)/ti["line_period_s"]
    out=np.empty_like(image); rows=np.arange(c["ny"])
    for k,offset in enumerate(offsets):
        out[:,k]=np.interp(rows,rows+offset,image[:,k])
    return out


def optical_reference(blurred: np.ndarray,c:dict[str,Any]) -> np.ndarray:
    ti=timing(c); spp=c["samples_per_pixel"]
    xs=(np.arange(c["nx"]*spp)+.5)/spp-.5
    vals=np.empty((c["ny"],c["nx"]))
    to=(np.arange(c["nx"]*spp)+.5)/c["adc_fs_hz"]
    for j in range(c["ny"]):
        ys=np.full_like(xs,float(j))
        if c["continuous_feed"]:
            ys+=(to-ti["active_s"]/2)/ti["line_period_s"]
        vals[j]=sample_scene(blurred,xs,ys,c).reshape(c["nx"],spp).mean(axis=1)
    return -np.log10(np.maximum(rectify_feed(vals,c),1e-12))


def run(c:dict[str,Any]) -> dict[str,Any]:
    validate_config(c);rng=np.random.default_rng(c["seed"])
    d,tr=make_phantom(c); blurred=optical_blur(tr,c)
    whites=[];blacks=[]
    # Synthetic, noiseless reference scans (one per facet). Real calibration must be measured and averaged.
    for f in range(c["facets"]):
        whites.append(simulate_line(f,blurred,c,rng,"white")["current"])
        blacks.append(simulate_line(f,blurred,c,rng,"opaque")["current"])
    measured=np.empty((c["ny"],c["nx"])); flags=np.zeros_like(measured,dtype=bool)
    traces=[];active=[]; adc_clips=0;pmt_clips=0
    for j in range(c["ny"]):
        r=simulate_line(j,blurred,c,rng,keep_trace=j<c["trace_lines"])
        f=j%c["facets"]
        denominator=whites[f]-blacks[f]
        if np.any(denominator<=0):
            raise RuntimeError("Calibration white <= opaque; check gain, rail limits and log model")
        measured[j]=(r["current"]-blacks[f])/denominator
        flags[j]=r["saturated"]
        active.append(r["active_codes"])
        adc_clips+=r["adc_clip_count"];pmt_clips+=r["pmt_clip_count"]
        if "trace" in r:traces.append(r["trace"])
    t=rectify_feed(measured,c)
    invalid=(t<=0)|flags
    # Numerical floor only prevents NaN in preview. Invalid pixels remain flagged, not hidden.
    dens=-np.log10(np.maximum(t,1e-8))
    ref=optical_reference(blurred,c)
    roi=np.ones_like(invalid);roi[:2]=False;roi[-2:]=False;roi[:,:2]=False;roi[:,-2:]=False
    good=roi&~invalid
    error=dens-ref
    metrics=dict(configuration_label=c["label"],seed=c["seed"],shape=list(dens.shape),
                 timing=timing(c),density_rmse_valid_interior=float(np.sqrt(np.mean(error[good]**2))),
                 density_mae_valid_interior=float(np.mean(np.abs(error[good]))),
                 invalid_fraction=float(np.mean(invalid)),adc_clipped_samples=adc_clips,
                 pmt_clipped_substeps=pmt_clips,raw_active_sample_count=int(sum(len(a) for a in active)),
                 reference="Optical PSF + finite sampling + first-order feed rectification; not unblurred OD truth",
                 calibration="Synthetic noiseless white/opaque scans; not measured calibration",
                 implementation="Behavioural optical/electronic chain. No optical prescription, SPI, I/O timing closure or DMA driver.")
    patch=[]
    for k in range(11):
        x0=int((k+.25)*c["nx"]/11);x1=max(x0+1,int((k+.75)*c["nx"]/11))
        a=dens[max(2,int(.06*c["ny"])):max(3,int(.20*c["ny"])),x0:x1]
        b=invalid[max(2,int(.06*c["ny"])):max(3,int(.20*c["ny"])),x0:x1]
        valid_a=a[~b]
        patch.append(dict(nominal_density=k*c["density_max"]/10,
            mean=float(np.mean(valid_a)) if valid_a.size else None,
            std=float(np.std(valid_a)) if valid_a.size else None,
            invalid_fraction=float(b.mean())))
    metrics["density_patches"]=patch
    return dict(config=c,truth_density=d,blurred_transmission=blurred,density=dens,
                transmission=t,invalid=invalid,optical_reference_density=ref,
                trace=traces,active_codes=active,metrics=metrics,
                white_current=np.array(whites),opaque_current=np.array(blacks))


def write_trace(path:Path,traces:list[dict[str,np.ndarray]]) -> None:
    with path.open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.writer(f)
        if not traces:return
        keys=list(traces[0]);w.writerow(keys)
        for tr in traces:
            for row in zip(*(tr[k] for k in keys)):
                w.writerow([float(v) if np.issubdtype(type(v),np.floating) else int(v) for v in row])


def fifo_budget(arrivals:np.ndarray,service:np.ndarray,capacity:float) -> tuple[np.ndarray,int]:
    """Conservative bucket queue, service then arrival each tick; ADC never backpressured."""
    q=0.;overflow=0;hist=[]
    for a,s in zip(arrivals,service):
        q=max(0.,q-float(s))+float(a)
        if q>capacity:overflow+=1
        hist.append(q)  # Do not clamp: retains required storage for sizing.
    return np.array(hist),overflow
