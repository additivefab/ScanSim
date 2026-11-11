from pathlib import Path
import copy
import numpy as np
import pytest
from src.scanner_sim import *
ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture
def cfg():return load_config(ROOT/'config/demo.json')

def test_geometry(cfg):
 t=timing(cfg)
 assert t['line_rate_hz']==1000
 assert np.isclose(t['vx_m_s'],500)
 assert np.isclose(t['frame_scan_s'],.128)
 assert np.isclose(t['active_s'],25.6e-6)
 assert np.isclose(2*t['equivalent_f_m']*2*np.pi*cfg['rpm']/60,500)

def test_log_roundtrip(cfg):
 i=np.geomspace(1e-10,5e-5,400)
 assert np.max(np.abs(inverse_log(log_voltage(i,cfg),cfg)/i-1))<1e-12

def test_quantization_bounds(cfg):
 x=np.linspace(-1.2,1.2,1001);code,flag=quantize(x,cfg)
 assert code.min()==0 and code.max()==65535
 lsb=2/65536;inside=~flag
 assert np.max(abs(code_to_v(code,cfg)[inside]-x[inside]))<=lsb/2+1e-12

def test_transmission_not_density_blur(cfg):
 cfg=copy.deepcopy(cfg);cfg['phantom_supersample']=1;cfg['spot_fwhm_um']=100.
 d=np.zeros((20,40));d[:,20:]=3
 right=-np.log10(optical_blur(10**(-d),cfg))
 wrong=optical_blur(d,cfg)
 assert abs(right[10,19]-wrong[10,19])>.1

def test_constant_psf_preserved(cfg):
 out=optical_blur(np.full((50,50),.003),cfg)
 assert np.allclose(out,.003)

def test_pipeline_alignment(cfg):
 a=np.arange(50);valid=a%7==0
 b=delayed(a,12,-1);v=delayed(valid,12,False)
 assert np.all(b[:12]==-1)
 assert np.all(b[12:]==a[:-12])
 assert np.all(v[12:]==valid[:-12])
 assert not v[:12].any()

def test_fifo_conservation_no_clamping():
 arrivals=np.ones(30)*2;service=np.ones(30)
 q,ov=fifo_budget(arrivals,service,100)
 assert ov==0 and q[-1]==31
 q,ov=fifo_budget(arrivals,np.zeros(30),16)
 assert ov>0 and q[-1]==60

def test_ideal_end_to_end():
 c=load_config(ROOT/'config/ideal.json');c.update(nx=64,ny=32,trace_lines=1)
 r=run(c)
 assert r['density'].shape==(32,64)
 assert r['metrics']['raw_active_sample_count']==64*32*2
 assert r['metrics']['density_rmse_valid_interior']<.002
 assert r['metrics']['adc_clipped_samples']==0

def test_reproducible_seed(cfg):
 c=copy.deepcopy(cfg);c.update(nx=32,ny=12,trace_lines=0)
 a=run(c);b=run(c)
 assert np.array_equal(a['density'],b['density'])

def test_invalid_rate_rejected(cfg):
 cfg['adc_fs_hz']=1e6
 with pytest.raises(ValueError):validate_config(cfg)

def test_window_overrun_rejected(cfg):
 cfg['nx']=20000
 with pytest.raises(ValueError):validate_config(cfg)

def test_lowpass_dc(cfg):
 y=lowpass(np.full(1000,3.),2e6,6.25e-9,3.)
 assert np.max(abs(y-3))<1e-12

def test_sampling_vs_bus_tag_count(cfg):
 c=copy.deepcopy(cfg);c.update(nx=64,ny=8,trace_lines=1)
 r=run(c);t=r['trace'][0]
 assert t['bus_valid'].sum()==128
 selected=t['bus_pixel_col'][t['bus_valid']>0]
 assert selected.min()==0 and selected.max()==63
 assert np.all(np.bincount(selected)==2)

def test_raw_endianness_roundtrip(tmp_path):
 v=np.array([1,256,65535],dtype='<u2');p=tmp_path/'a.raw';v.tofile(p)
 assert p.read_bytes()==b'\x01\x00\x00\x01\xff\xff'
 assert np.array_equal(np.fromfile(p,dtype='<u2'),v)
