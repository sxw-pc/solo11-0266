import json,tempfile,unittest
from pathlib import Path
import usagecourt
from usagecourt.store import connect,run,replay,one,rows

ROOT=Path(__file__).resolve().parents[1]

class UsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'db.sqlite';self.db=connect(str(self.path))
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def fixture(self,sub,kind):
        return [run(self.db,sub,x) for x in json.loads((ROOT/'fixtures'/f'{sub}_{kind}.json').read_text(encoding='utf-8'))]
    def start(self):self.fixture('tariff','normal');self.fixture('meter','normal')
    def test_dual_approval_price_windows_and_conflict(self):
        self.assertEqual(self.fixture('tariff','normal')[-1]['decision'],'discount_active')
        self.assertEqual(self.fixture('tariff','failure')[0]['decision'],'overlapping_price')
        self.assertEqual(one(self.db,"SELECT status FROM discounts WHERE id='discount-a'")['status'],'active')
    def test_uid_and_cluster_overlap_change_meter_decisions(self):
        self.start();results=self.fixture('meter','failure')
        self.assertEqual([r['decision'] for r in results[:3]],['sample_denied','overlap_dispute','evidence_gap'])
        self.assertEqual(one(self.db,"SELECT status FROM meter_cases WHERE id='meter-a'")['status'],'review')
    def test_price_generation_splits_same_meter_case(self):
        self.start();result=run(self.db,'bill',{'event_id':'issue-test','op':'issue','invoice':'invoice-test','case':'meter-a','tenant':'tenant-a'})
        self.assertEqual((result['decision'],result['lines'],result['total']),('invoice_issued',2,90))
        lines=rows(self.db,"SELECT price,rate,amount FROM invoice_lines WHERE invoice='invoice-test' ORDER BY start")
        self.assertEqual([l['price'] for l in lines],['cpu-v1','cpu-v2'])
        self.assertEqual([l['amount'] for l in lines],[36,54])
    def test_dispute_freezes_only_line_and_reversal_preserves_original(self):
        self.start();results=self.fixture('bill','normal')
        self.assertEqual(results[0]['total'],90)
        self.assertEqual(results[-1],{'decision':'invoice_settled','total':64})
        original=one(self.db,"SELECT * FROM invoice_lines WHERE id='invoice-a:1'")
        reverse=one(self.db,"SELECT * FROM invoice_lines WHERE id='resolve-1:reverse'")
        self.assertEqual((original['status'],original['amount'],reverse['amount']),('reversed',36,-36))
    def test_outage_reopens_settlement_and_requires_reconciliation(self):
        self.start();self.fixture('bill','normal');self.fixture('meter','failure')
        self.assertEqual(one(self.db,"SELECT status FROM settlements WHERE id='settle-1'")['status'],'review')
        self.assertEqual(self.fixture('bill','failure')[0]['decision'],'invoice_blocked')
        self.assertEqual(one(self.db,"SELECT status FROM settlements WHERE id='settle-2'")['status'],'settled')
    def test_same_name_new_uid_cannot_inherit_old_meter(self):
        self.start();self.fixture('bill','normal')
        self.assertEqual(run(self.db,'meter',{'event_id':'rebuild','op':'identity','id':'pod-a','namespace':'checkout','uid':'uid-b','tenant':'tenant-b','generation':2,'cluster':'west'})['decision'],'identity_recorded')
        self.assertEqual(one(self.db,"SELECT status FROM settlements WHERE id='settle-1'")['status'],'review')
        self.assertEqual(run(self.db,'meter',{'event_id':'old-seal','op':'seal','id':'old-seal','identity':'pod-a','sample':'sample-a','resource':'cpu','start':40,'ends':60})['decision'],'identity_or_sample_changed')
    def test_discount_revocation_reopens_settled_invoice(self):
        self.start();self.fixture('bill','normal')
        self.assertEqual(run(self.db,'tariff',{'event_id':'discount-revoke','op':'revoke_discount','id':'discount-a'})['decision'],'discount_revoked')
        self.assertEqual(one(self.db,"SELECT status FROM settlements WHERE id='settle-1'")['status'],'review')
    def test_sample_correction_reopens_only_referenced_invoice(self):
        self.start();self.fixture('bill','normal')
        result=run(self.db,'meter',{'event_id':'correct-1','op':'correct','id':'sample-a-v2','old':'sample-a','units':3,'source':'meter-audit'})
        self.assertEqual(result['decision'],'evidence_corrected')
        self.assertEqual(one(self.db,"SELECT status FROM settlements WHERE id='settle-1'")['status'],'review')
        self.assertEqual(one(self.db,"SELECT status FROM meter_cases WHERE id='meter-a'")['status'],'review')
    def test_six_fixtures_event_conflict_and_replay(self):
        for sub,kind in [('tariff','normal'),('meter','normal'),('bill','normal'),('tariff','failure'),('meter','failure'),('bill','failure')]:self.fixture(sub,kind)
        command=json.loads((ROOT/'fixtures'/'tariff_normal.json').read_text(encoding='utf-8'))[0]
        self.assertEqual(run(self.db,'tariff',command)['decision'],'price_active')
        self.assertEqual(run(self.db,'tariff',dict(command,rate=999))['decision'],'event_identity_conflict')
        result=replay(str(self.path))
        self.assertTrue(result['verified'],result['divergences'])
        self.assertEqual(result['decisions'],26)
    def test_module_is_this_repo(self):self.assertEqual(Path(usagecourt.__file__).resolve().parents[1],ROOT)

print('module_file',usagecourt.__file__)
