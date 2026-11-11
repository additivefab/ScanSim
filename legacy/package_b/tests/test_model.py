import copy, json, tempfile, unittest
from pathlib import Path
import numpy as np
from src.scanner import (Q,load_config,phantom,static_voltage,inverse_voltage,
                        quantize,code_voltage,compound_counts,budget,run,validate)
ROOT=Path(__file__).resolve().parents[1]
class ModelTests(unittest.TestCase):
    def setUp(self): self.c=load_config(ROOT/'configs/ideal_regression.json')
    def test_density_transmission_roundtrip(self):
        d=np.linspace(0,5,100);np.testing.assert_allclose(-np.log10(10**-d),d,atol=1e-12)
    def test_log_inverse(self):
        i=np.geomspace(1e-10,1e-4,100)
        np.testing.assert_allclose(inverse_voltage(static_voltage(i,self.c['analog']),self.c['analog']),i,rtol=1e-12)
    def test_linear_inverse(self):
        a=copy.deepcopy(self.c['analog']);a['mode']='linear';i=np.geomspace(1e-10,1e-4,100)
        np.testing.assert_allclose(inverse_voltage(static_voltage(i,a),a),i,rtol=1e-10)
    def test_adc_limits(self):
        d=self.c['adc'];out=quantize(np.array([-1,0,d['vmax'],d['vmax']+1]),d)
        np.testing.assert_array_equal(out,[0,0,65535,65535])
    def test_adc_midbin_error(self):
        d=self.c['adc'];v=np.linspace(.01,2.49,1000)
        self.assertLessEqual(np.max(np.abs(code_voltage(quantize(v,d),d)-v)),(d['vmax']-d['vmin'])/2**(d['bits']+1)+1e-14)
    def test_poisson_gain_statistics(self):
        mu=np.full(200000,15.0);q=compound_counts(mu,1.3,np.random.default_rng(7))
        self.assertAlmostEqual(q.mean(),15,delta=.06);self.assertAlmostEqual(q.var(),19.5,delta=.25)
    def test_phantom_shape_range(self):
        d=phantom(64,16,3);self.assertEqual(d.shape,(16,64));self.assertTrue(((d>=0)&(d<=3)).all())
    def test_invalid_roi(self):
        c=copy.deepcopy(self.c);c['scan']['line_samples']=70
        with self.assertRaises(ValueError): validate(c)
    def test_budget(self):
        c=load_config(ROOT/'configs/reference_budget.json');b=budget(c)
        self.assertEqual(b['nx'],4000);self.assertEqual(b['ny'],5000)
        self.assertAlmostEqual(b['raster_duration_s'],5);self.assertEqual(b['raw_frame_bytes'],40000000)
    def test_ideal_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            m=run(self.c,Path(d));self.assertLess(m['density_rmse_unblurred'],3e-5)
            self.assertEqual(m['adc_clip_active'],0);self.assertEqual(m['pmt_clip_internal'],0)
            z=np.load(Path(d)/'simulation_arrays.npz');self.assertEqual(z['raw_codes'].shape,(16,64))
    def test_adc_tag_alignment(self):
        with tempfile.TemporaryDirectory() as p:
            run(self.c,Path(p));trace=np.loadtxt(Path(p)/'line_trace.csv',delimiter=',',skiprows=1)
            L=self.c['adc']['latency_samples'];pre=self.c['scan']['pre_samples'];nx=self.c['scan']['nx']
            valid=trace[:,13].astype(bool)
            np.testing.assert_array_equal(trace[valid,15],np.arange(nx))
            np.testing.assert_array_equal(trace[valid,14],trace[pre:pre+nx,11])
            self.assertEqual(int(np.flatnonzero(valid)[0]),pre+L)
    def test_ideal_linear(self):
        c=copy.deepcopy(self.c);c['analog']['mode']='linear';c['analog']['offset_V']=.1
        with tempfile.TemporaryDirectory() as d:
            m=run(c,Path(d));self.assertLess(m['density_rmse_unblurred'],.004)
    def test_multirate_constant_density(self):
        c=copy.deepcopy(self.c);c['scan']['samples_per_pixel']=5
        c['scan']['line_samples']=1024
        with tempfile.TemporaryDirectory() as d:
            path=Path(d);truth=np.full((16,64),1.234);np.save(path/'density.npy',truth)
            m=run(c,path/'out',path/'density.npy')
            self.assertLess(m['density_rmse_unblurred'],4e-5)
            z=np.load(path/'out/simulation_arrays.npz')
            self.assertEqual(z['raw_codes'].shape,(16,320));self.assertEqual(z['reconstructed'].shape,(16,64))
    def test_differential_driver_inverse(self):
        c=copy.deepcopy(self.c);c['analog'].update(adc_driver_gain=.8,adc_driver_offset_V=-1.)
        c['adc'].update(vmin=-1.,vmax=1.)
        with tempfile.TemporaryDirectory() as p:
            m=run(c,Path(p));self.assertLess(m['density_rmse_unblurred'],3e-5)
if __name__=='__main__': unittest.main(verbosity=2)
