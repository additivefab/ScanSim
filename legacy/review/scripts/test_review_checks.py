"""Unit tests for review helpers only. These are NOT the V1 14 tests."""
import json
import unittest
from pathlib import Path
from review_checks import bandwidth, frame_bytes, queue_trace, measurement_gate, observable_event_gate, hardware_gate
ROOT=Path(__file__).resolve().parents[1]
class ReviewChecks(unittest.TestCase):
    def test_01_dual125_rate(self):self.assertEqual(bandwidth(125e6,2)['raw_Bps'],500000000)
    def test_02_single20_rate(self):self.assertEqual(bandwidth(20e6,1)['raw_Bps'],40000000)
    def test_03_overrate_fails(self):self.assertEqual(bandwidth(125e6,2,output_Bps=125e6)['average_gate'],'FAIL')
    def test_04_no_output_is_unknown(self):self.assertEqual(bandwidth(20e6,1)['average_gate'],'UNRESOLVED')
    def test_05_equal_rate_no_margin(self):self.assertEqual(bandwidth(50e6,1,output_Bps=100e6)['average_gate'],'FAIL')
    def test_06_window_not_free_margin(self):self.assertEqual(bandwidth(125e6,2,keep_fraction=.2,overhead_fraction=.05,output_Bps=100e6)['average_gate'],'FAIL')
    def test_07_decimation_rate(self):self.assertEqual(bandwidth(40e6,2,decimation=2)['kept_Bps'],80000000)
    def test_08_invalid_rate(self):
        with self.assertRaises(ValueError):bandwidth(float('nan'),1)
    def test_09_invalid_fraction(self):
        with self.assertRaises(ValueError):bandwidth(20e6,1,keep_fraction=1.2)
    def test_10_frame_single(self):self.assertEqual(frame_bytes(7000,8600,1,2),120400000)
    def test_11_frame_dual(self):self.assertEqual(frame_bytes(7000,8600,2,2),240800000)
    def test_12_invalid_frame(self):
        with self.assertRaises(ValueError):frame_bytes(7000,0,1,2)
    def test_13_queue_conservation(self):
        q=queue_trace([('arrival',10),('service',4),('arrival',8),('service',5)],12)
        self.assertEqual(q,{'produced':18,'consumed':9,'lost':2,'queued':7,'peak':12})
    def test_14_queue_service_not_saved(self):
        q=queue_trace([('service',100),('arrival',10)],20)
        self.assertEqual(q['queued'],10)
    def test_15_two_scope_channels_reject_three(self):self.assertEqual(measurement_gate(3,2),'FAIL')
    def test_16_repeated_is_not_simultaneous(self):self.assertEqual(measurement_gate(3,2,False),'NON_SIMULTANEOUS')
    def test_17_true_facet_blocked(self):self.assertEqual(observable_event_gate({'observability':'measured','origin':'fake','facet_id_true':3}),'FAIL')
    def test_18_sim_only_blocked(self):self.assertEqual(observable_event_gate({'observability':'sim_only','origin':'truth'}),'FAIL')
    def test_19_profile_stays_unresolved(self):
        cfg=json.loads((ROOT/'configs/硬件配置草案_未知项禁止自动补齐.json').read_text())
        self.assertEqual(hardware_gate(cfg)['status'],'UNRESOLVED')
    def test_20_pin_registry_versions_and_roles(self):
        rows=json.loads((ROOT/'tables/pin_registry.json').read_text())
        def get(rev,cn,p):return next(x for x in rows if x['board_revision']==rev and x['connector']==cn and x['physical_pin']==p)
        self.assertEqual(len(rows),120)
        for rev,cn in [('V1.3/V1.3B','J5'),('V1.3/V1.3B','J6'),('V1.0/V1.1','J5')]:
            subset=[x for x in rows if x['board_revision']==rev and x['connector']==cn]
            self.assertEqual(sum(x['net_role']=='GPIO' for x in subset),34)
            self.assertEqual(len({x['physical_pin'] for x in subset}),40)
        self.assertEqual(get('V1.3/V1.3B','J5',1)['net_role'],'5V_DC')
        self.assertEqual(get('V1.0/V1.1','J5',1)['package_pin'],'H19')
        self.assertEqual(get('V1.3/V1.3B','J5',7)['package_pin'],'H19')
        self.assertEqual(get('V1.3/V1.3B','J6',1)['package_pin'],'U22')
if __name__=='__main__':unittest.main(verbosity=2)
