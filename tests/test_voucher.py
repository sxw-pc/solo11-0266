import json,tempfile,unittest
from pathlib import Path
from usagecourt.store import connect,run,replay,one,rows

ROOT=Path(__file__).resolve().parents[1]

class VoucherTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'db.sqlite';self.db=connect(str(self.path))
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def fx(self,sub,kind):
        return [run(self.db,sub,x) for x in json.loads((ROOT/'fixtures'/f'{sub}_{kind}.json').read_text(encoding='utf-8'))]
    def start_voucher(self):
        for s,k in [('tariff','normal'),('meter','normal'),('tariff','voucher'),('meter','voucher'),('bill','voucher')]:
            self.fx(s,k)
    def start_trap(self):
        self.start_voucher()
        for s,k in [('tariff','trap'),('meter','trap'),('bill','trap')]:
            self.fx(s,k)
    def auth(self,event,mode,invoice='invoice-z',tenant='tenant-a',at=90,**extra):
        return run(self.db,'bill',{'event_id':event,'op':'authorize','id':event,'invoice':invoice,'tenant':tenant,'mode':mode,'at':at,**extra})

    def test_two_line_bill_is_36_and_54(self):
        self.start_voucher()
        lines=rows(self.db,"SELECT price,amount FROM invoice_lines WHERE invoice='invoice-z' ORDER BY start")
        self.assertEqual([l['price'] for l in lines],['mem-v1','mem-v2'])
        self.assertEqual([l['amount'] for l in lines],[36,54])

    def test_reduce_authorization_payable_14_and_no_renewable(self):
        self.start_voucher()
        r=self.auth('red-1','reduce')
        self.assertEqual(r['decision'],'voucher_authorized')
        self.assertEqual((r['gross'],r['voucher_deduction'],r['payable']),(90,76,14))
        # 36 voucher fully covers the v1 line; the whole 40 group lands on the v2 line
        byline={x['line']:x for x in r['redemptions']}
        self.assertEqual(sum(x['amount'] for x in r['redemptions'] if x['line']=='invoice-z:1'),36)
        self.assertEqual(sum(x['amount'] for x in r['redemptions'] if x['line']=='invoice-z:2'),40)
        # nothing left for renewal after a reduce authorization
        self.assertEqual(r['renewal_remaining'],0)

    def test_renew_authorization_payable_54_and_keeps_commitment(self):
        self.start_voucher()
        r=self.auth('ren-1','renew')
        self.assertEqual(r['decision'],'voucher_authorized')
        # the 40 group is reserved whole for the future commitment, only the 36 voucher reduces the bill
        self.assertEqual((r['gross'],r['voucher_deduction'],r['payable']),(90,36,54))
        self.assertEqual(r['renewal_remaining'],40)
        self.assertEqual(sorted(x['voucher'] for x in r['reservations']),['voucher-g:1','voucher-g:2'])
        self.assertEqual(sum(x['amount'] for x in r['reservations']),40)
        # every reservation references the v2 version the commitment requires
        self.assertTrue(all(one(self.db,"SELECT 1 FROM vouchers WHERE id=? AND (price IS NULL OR price='mem-v2')",(x['voucher'],)) for x in r['reservations']))

    def test_repeated_submission_does_not_deduct_twice(self):
        self.start_voucher()
        first=self.auth('red-1','reduce')
        # identical event id replays the stored decision
        self.assertEqual(self.auth('red-1','reduce'),first)
        # a fresh authorization against the same invoice is refused
        again=self.auth('red-2','reduce')
        self.assertEqual(again['decision'],'voucher_invoice_already_redeemed')
        self.assertEqual(one(self.db,"SELECT COALESCE(SUM(amount),0) n FROM voucher_redemptions WHERE status='active'")['n'],76)

    def test_version_locked_voucher_cannot_cross_lines(self):
        self.start_voucher()
        r=self.auth('red-1','reduce')
        # the mem-v1-locked 36 voucher is never applied to the mem-v2 line
        self.assertFalse(any(x['voucher']=='voucher-36' and x['line']=='invoice-z:2' for x in r['redemptions']))

    def test_grouped_subvouchers_move_as_a_whole(self):
        self.start_voucher()
        r=self.auth('red-1','reduce')
        moved={x['voucher'] for x in r['redemptions']}
        # both members of group-g are redeemed together for their full faces
        self.assertIn('voucher-g:1',moved);self.assertIn('voucher-g:2',moved)
        for vid in ('voucher-g:1','voucher-g:2'):
            self.assertEqual(one(self.db,"SELECT amount FROM voucher_redemptions WHERE voucher=? AND status='active'",(vid,))['amount'],20)

    def test_surface_balance_trap_requires_split_and_is_refused(self):
        self.start_trap()
        line=one(self.db,"SELECT * FROM invoice_lines WHERE invoice='invoice-trap'")
        # two grouped 30 sub-vouchers look sufficient against a 40 line, but neither can be wholly absorbed
        self.assertEqual(line['amount'],40)
        r=self.auth('trap-1','reduce',invoice='invoice-trap',tenant='tenant-b')
        self.assertEqual(r['decision'],'voucher_allocation_denied')
        self.assertEqual(r['payable'],40)
        self.assertEqual(one(self.db,"SELECT COUNT(*) n FROM voucher_redemptions")['n'],0)

    def test_review_blocks_unexecuted_redemption(self):
        self.start_voucher()
        run(self.db,'meter',{'event_id':'corr','op':'correct','id':'sample-z-v2','old':'sample-z','units':3,'source':'audit'})
        self.assertEqual(self.auth('blocked-1','reduce')['decision'],'invoice_under_review')
        self.assertEqual(run(self.db,'bill',{'event_id':'set-block','op':'settle','id':'set-block','invoice':'invoice-z','tenant':'tenant-a','at':92})['decision'],'invoice_blocked')

    def test_release_quarantines_balance_away_from_other_invoices(self):
        self.start_voucher()
        self.auth('red-1','reduce')
        run(self.db,'meter',{'event_id':'corr','op':'correct','id':'sample-z-v2','old':'sample-z','units':3,'source':'audit'})
        rel=run(self.db,'bill',{'event_id':'rel-1','op':'release_redemption','id':'rel-1','invoice':'invoice-z','tenant':'tenant-a'})
        self.assertEqual((rel['decision'],rel['restored']),('redemption_reversed',76))
        # a second clean invoice cannot spend the quarantined balance in the meantime
        run(self.db,'meter',{'event_id':'s2','op':'sample','id':'sample-z2','identity':'pod-a','uid':'uid-a','tenant':'tenant-a','resource':'memory','cluster':'east','start':0,'ends':30,'units':2,'source':'node'})
        run(self.db,'meter',{'event_id':'c2','op':'seal','id':'meter-z2','identity':'pod-a','sample':'sample-z2','resource':'memory','start':10,'ends':30})
        run(self.db,'bill',{'event_id':'i2','op':'issue','invoice':'invoice-z2','case':'meter-z2','tenant':'tenant-a'})
        other=self.auth('other-1','reduce',invoice='invoice-z2')
        self.assertEqual(other['decision'],'voucher_allocation_denied')

    def test_reissue_then_reauthorize_restores_net_due_and_inventory(self):
        self.start_voucher()
        self.auth('red-1','reduce')
        run(self.db,'meter',{'event_id':'corr','op':'correct','id':'sample-z-v2','old':'sample-z','units':3,'source':'audit'})
        run(self.db,'bill',{'event_id':'rel-1','op':'release_redemption','id':'rel-1','invoice':'invoice-z','tenant':'tenant-a'})
        ri=run(self.db,'bill',{'event_id':'ri-1','op':'reissue_reviewed','id':'ri-1','invoice':'invoice-z','tenant':'tenant-a'})
        self.assertEqual(ri['decision'],'invoice_reissued')
        # original redemptions keep their original line/voucher mapping, now marked reversed
        self.assertEqual(one(self.db,"SELECT COUNT(*) n FROM voucher_redemptions WHERE invoice='invoice-z' AND status='reversed'")['n'],3)
        ra=run(self.db,'bill',{'event_id':'ra-1','op':'reauthorize','id':'ra-1','invoice':'invoice-z','tenant':'tenant-a','mode':'reduce','at':94})
        self.assertEqual(ra['decision'],'voucher_reauthorized')
        self.assertEqual(ra['payable'],14)
        settle=run(self.db,'bill',{'event_id':'set-1','op':'settle','id':'set-1','invoice':'invoice-z','tenant':'tenant-a','at':95})
        self.assertEqual((settle['decision'],settle['net_due'],settle['voucher_deduction']),('invoice_settled',14,76))
        # the recomputed free balance matches the original 76 redeemed once more (no leakage)
        self.assertEqual(one(self.db,"SELECT COALESCE(SUM(amount),0) n FROM voucher_redemptions WHERE invoice='invoice-z' AND status='active'")['n'],76)

    def test_renew_review_cycle_releases_and_repromises_reservations(self):
        self.start_voucher()
        r=self.auth('ren-1','renew')
        self.assertEqual(r['renewal_remaining'],40)
        run(self.db,'meter',{'event_id':'corr','op':'correct','id':'sample-z-v2','old':'sample-z','units':3,'source':'audit'})
        rel=run(self.db,'bill',{'event_id':'rel-1','op':'release_redemption','id':'rel-1','invoice':'invoice-z','tenant':'tenant-a'})
        self.assertEqual((rel['restored'],rel['reservations_released']),(36,40))
        run(self.db,'bill',{'event_id':'ri-1','op':'reissue_reviewed','id':'ri-1','invoice':'invoice-z','tenant':'tenant-a'})
        ra=run(self.db,'bill',{'event_id':'ra-1','op':'reauthorize','id':'ra-1','invoice':'invoice-z','tenant':'tenant-a','mode':'renew','at':94})
        self.assertEqual(ra['decision'],'voucher_reauthorized')
        self.assertEqual((ra['payable'],ra['renewal_remaining']),(54,40))

    def test_expired_and_future_vouchers_are_excluded(self):
        self.start_voucher()
        self.assertEqual(self.auth('late','reduce',at=131)['decision'],'voucher_allocation_denied')
        run(self.db,'bill',{'event_id':'fv','op':'voucher_buy','id':'future-v','batch':'bf','tenant':'tenant-a','resource':'memory','price':None,'face':100,'starts':95,'ends':200})
        r=self.auth('early','reduce',at=90)
        # the not-yet-valid 100 voucher must not be counted; only the original 76 is deductible
        self.assertEqual(r['voucher_deduction'],76)

    def test_two_commitments_cannot_share_one_balance(self):
        self.start_voucher()
        # a second v2 commitment that the inventory cannot also cover once group-g is promised
        run(self.db,'bill',{'event_id':'c2','op':'commitment_register','id':'commit-extra','tenant':'tenant-a','resource':'memory','price':'mem-v2','amount':10,'starts':100,'ends':120})
        r=self.auth('ren-2','renew')
        self.assertEqual(r['decision'],'voucher_allocation_denied')
        self.assertIn('commit-extra',r['uncovered'])
        self.assertEqual(r['payable'],90)

    def test_replay_restores_voucher_state(self):
        self.start_trap()
        self.auth('red-1','reduce')
        self.db.close()
        result=replay(str(self.path))
        self.assertTrue(result['verified'],result['divergences'])

if __name__=='__main__':
    unittest.main()
