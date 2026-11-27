"""Calibrated handoff / dual-authorization sealing regression.

Acceptance scenario (same device trace across a tariff price cut):

* primary probe covers 40..52, backup covers 45..60;
* the 45..52 overlap contains the price cut at 50;
* both probes total 6 units over the overlap, but split 2/4 vs 4/2 across
  the price segments -> calibration fails and backup authorization leaves a
  52..60 evidence gap too;
* an equal-split backup calibrates with candidate handoffs at 50 and 52;
  52 is chosen (farthest provable continuation);
* strict authorization bills only to 52 and cannot settle the full window;
* backup authorization settles the whole window to 60;
* a late correction destroys only the 52 handoff candidate; replanning moves
  the handoff to 50, freezing only the bill line whose amount/provenance
  changed while the other line stays issued;
* the whole state is replayable from the identical event set.
"""
import tempfile
import unittest
from pathlib import Path

from usagecourt.store import connect, run, replay, one, rows

# Per-atomic-slot traces (slot t is the interval [t, t+1)).
MAIN = [1, 1, 1, 1, 1, 0, 0, 1, 0, 1, 2, 2]                 # 40..52: overlap 2 then 4
BACKUP_SWAPPED = [1, 1, 1, 1, 0, 1, 1] + [1] * 8            # 45..60: overlap 4 then 2
BACKUP_EQUAL = [0, 0, 1, 0, 1, 2, 2] + [1] * 8              # 45..60: overlap 2 then 4
MAIN_CORRECTED = [1, 1, 1, 1, 1, 0, 0, 1, 0, 1, 3, 2]       # t=50 changed: kills handoff 52

assert len(MAIN) == 12 and len(BACKUP_SWAPPED) == 15
assert len(BACKUP_EQUAL) == 15 and len(MAIN_CORRECTED) == 12


class CalibrationHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'cal.sqlite')
        self.db = connect(self.path)
        self.seq = 0

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def ev(self, subsystem, payload):
        self.seq += 1
        payload = {'event_id': f'e{self.seq}', **payload}
        return run(self.db, subsystem, payload)

    def tariff_and_identity(self):
        self.ev('tariff', {'op': 'price', 'id': 'cpu-v1', 'tenant': 't', 'resource': 'cpu',
                           'generation': 1, 'start': 0, 'ends': 50, 'rate': 2})
        self.ev('tariff', {'op': 'price', 'id': 'cpu-v2', 'tenant': 't', 'resource': 'cpu',
                           'generation': 2, 'start': 50, 'ends': 100, 'rate': 3})
        self.ev('meter', {'op': 'identity', 'id': 'pod', 'namespace': 'checkout',
                          'uid': 'uid-a', 'tenant': 't', 'generation': 1, 'cluster': 'east'})

    def probe_samples(self, main=MAIN, swapped=BACKUP_SWAPPED, equal=BACKUP_EQUAL):
        self.ev('meter', {'op': 'sample', 'id': 'main', 'identity': 'pod', 'uid': 'uid-a',
                          'tenant': 't', 'resource': 'cpu', 'cluster': 'east',
                          'start': 40, 'ends': 52, 'units': main, 'source': 'node-meter'})
        if swapped is not None:
            r = self.ev('meter', {'op': 'sample', 'id': 'bak-swapped', 'identity': 'pod',
                                  'uid': 'uid-a', 'tenant': 't', 'resource': 'cpu',
                                  'cluster': 'east', 'start': 45, 'ends': 60,
                                  'units': swapped, 'source': 'sidecar', 'role': 'backup'})
            # overlap trace is retained (and still answers overlap_dispute)
            self.assertEqual(r['decision'], 'overlap_dispute')
            self.assertIsNotNone(one(self.db, "SELECT id FROM samples WHERE id='bak-swapped'"))
        if equal is not None:
            r = self.ev('meter', {'op': 'sample', 'id': 'bak-equal', 'identity': 'pod',
                                  'uid': 'uid-a', 'tenant': 't', 'resource': 'cpu',
                                  'cluster': 'east', 'start': 45, 'ends': 60,
                                  'units': equal, 'source': 'sidecar', 'role': 'backup'})
            self.assertEqual(r['decision'], 'overlap_dispute')

    # ---- the headline acceptance test -------------------------------------

    def test_cross_price_cut_dual_authorization_and_targeted_correction(self):
        self.tariff_and_identity()
        self.probe_samples()

        # 1) swapped segmentation: totals agree (6) but per-price proof fails
        bad = self.ev('meter', {'op': 'calibrate', 'id': 'cal-bad', 'tenant': 't',
                                'identity': 'pod', 'uid': 'uid-a', 'resource': 'cpu',
                                'generation': 1, 'primary': 'main', 'backup': 'bak-swapped',
                                'handoffs': [50, 52]})
        self.assertEqual(bad['decision'], 'calibration_mismatch')
        self.assertEqual(bad['decision'], 'calibration_failed')  # vocabulary alias
        self.assertIsNone(bad['chosen'])
        reasons = {(c['handoff'], c['reason']) for c in bad['candidates']}
        self.assertIn((50, 'usage_mismatch'), reasons)
        self.assertIn((52, 'usage_mismatch'), reasons)
        # raw segment proof: 2 vs 4 and 4 vs 2 are both retained
        proof = rows(self.db, """SELECT cs.start,cs.ends,cs.primary_units,cs.backup_units,cs.equal
                                 FROM calibration_segments cs
                                 JOIN calibration_routes cr ON cr.id=cs.route
                                 WHERE cr.calibration='cal-bad' ORDER BY cs.start, cs.idx""")
        pairs = {(p['start'], p['ends']): (p['primary_units'], p['backup_units'], p['equal'])
                 for p in proof}
        self.assertEqual(pairs[(45, 50)], (2, 4, 0))
        self.assertEqual(pairs[(50, 52)], (4, 2, 0))

        # backup authorization without a usable credential leaves the same gap
        seal_bad = self.ev('meter', {'op': 'seal', 'id': 'case-bad', 'identity': 'pod',
                                     'sample': 'main', 'resource': 'cpu',
                                     'start': 40, 'ends': 60,
                                     'authorization': 'backup', 'calibration': 'cal-bad'})
        self.assertFalse(seal_bad['complete'])
        self.assertEqual(seal_bad['covered_to'], 52)
        self.assertEqual(seal_bad['gaps'], [{'start': 52, 'ends': 60}])

        # 2) equal segmentation calibrates; farthest handoff 52 is chosen
        good = self.ev('meter', {'op': 'calibrate', 'id': 'cal-good', 'tenant': 't',
                                 'identity': 'pod', 'uid': 'uid-a', 'resource': 'cpu',
                                 'generation': 1, 'primary': 'main', 'backup': 'bak-equal',
                                 'handoffs': [50, 52]})
        self.assertEqual(good['decision'], 'calibration_verified')
        self.assertEqual(good['decision'], 'calibrated')
        self.assertEqual(good['chosen']['handoff'], 52)
        self.assertEqual(good['chosen']['backup'], 'bak-equal')
        self.assertTrue(all(s['equal'] for s in good['chosen']['segments']))
        dbcal = one(self.db, "SELECT * FROM calibrations WHERE id='cal-good'")
        self.assertEqual(dbcal['handoff'], 52)
        self.assertEqual(dbcal['status'], 'active')

        # 3) strict authorization: proven primary evidence only -> bill to 52
        strict = self.ev('meter', {'op': 'seal', 'id': 'case-strict', 'identity': 'pod',
                                   'sample': 'main', 'resource': 'cpu',
                                   'start': 40, 'ends': 60, 'authorization': 'strict'})
        self.assertEqual(strict['decision'], 'meter_sealed')
        self.assertFalse(strict['complete'])
        self.assertEqual(strict['covered_to'], 52)
        self.assertEqual(rows(self.db, "SELECT start,ends FROM meter_gaps WHERE case_id='case-strict'"),
                         [{'start': 52, 'ends': 60}])

        issue_s = self.ev('bill', {'op': 'issue', 'invoice': 'inv-strict',
                                   'case': 'case-strict', 'tenant': 't'})
        self.assertEqual((issue_s['decision'], issue_s['lines'], issue_s['total']),
                         ('invoice_issued', 2, 26))
        slines = rows(self.db, "SELECT start,ends,quantity,rate,amount FROM invoice_lines "
                               "WHERE invoice='inv-strict' ORDER BY start")
        self.assertEqual([(l['start'], l['ends']) for l in slines], [(40, 50), (50, 52)])
        self.assertEqual([l['amount'] for l in slines], [14, 12])
        self.assertEqual(max(l['ends'] for l in slines), 52)

        blocked = self.ev('bill', {'op': 'settle', 'id': 'set-strict',
                                   'invoice': 'inv-strict', 'tenant': 't', 'at': 70})
        self.assertEqual(blocked['decision'], 'invoice_blocked')
        self.assertEqual(blocked['decision'], 'evidence_gap')  # threshold vocabulary
        self.assertIsNone(one(self.db, "SELECT id FROM settlements WHERE invoice='inv-strict'"))

        # 4) backup authorization settles the complete window to 60
        backup = self.ev('meter', {'op': 'seal', 'id': 'case-backup', 'identity': 'pod',
                                   'sample': 'main', 'resource': 'cpu',
                                   'start': 40, 'ends': 60,
                                   'authorization': 'backup', 'calibration': 'cal-good'})
        self.assertTrue(backup['complete'])
        self.assertEqual(backup['covered_to'], 60)
        self.assertEqual(backup['handoff'], 52)

        issue_b = self.ev('bill', {'op': 'issue', 'invoice': 'inv-backup',
                                   'case': 'case-backup', 'tenant': 't'})
        self.assertEqual((issue_b['lines'], issue_b['total']), (2, 50))
        blines = rows(self.db, "SELECT start,ends,quantity,amount FROM invoice_lines "
                               "WHERE invoice='inv-backup' ORDER BY start")
        self.assertEqual([(l['start'], l['ends'], l['quantity'], l['amount']) for l in blines],
                         [(40, 50, 7, 14), (50, 60, 12, 36)])
        # every charged unit is attributed to exactly one sample
        prov = rows(self.db, "SELECT sample,SUM(quantity) q FROM line_provenance "
                             "WHERE line LIKE 'inv-backup:%' GROUP BY sample ORDER BY sample")
        self.assertEqual([(p['sample'], p['q']) for p in prov],
                         [('bak-equal', 8), ('main', 11)])
        self.assertEqual(sum(p['q'] for p in prov), 19)

        settled = self.ev('bill', {'op': 'settle', 'id': 'set-backup',
                                    'invoice': 'inv-backup', 'tenant': 't', 'at': 71})
        self.assertEqual((settled['decision'], settled['total']), ('invoice_settled', 50))
        self.assertEqual(one(self.db, "SELECT status FROM settlements WHERE id='set-backup'")['status'],
                         'settled')

        # 5) late correction destroys only the 52 handoff candidate
        corr = self.ev('meter', {'op': 'correct', 'id': 'main-v2', 'old': 'main',
                                 'units': MAIN_CORRECTED, 'source': 'audit-correction'})
        self.assertEqual(corr['decision'], 'evidence_corrected')
        replans = {r['case']: r for r in corr['replans'] if r.get('case')}

        rb = replans['case-backup']
        self.assertEqual(rb['handoff'], 50)
        self.assertTrue(rb['complete'])
        self.assertEqual({f['line']: f['reason'] for f in rb['frozen_lines']},
                         {'inv-backup:2': 'provenance_changed'})
        self.assertEqual(rb['unchanged_lines'], ['inv-backup:1'])

        # line 1 stays issued; line 2 freezes on provenance even though amount is unchanged
        self.assertEqual(one(self.db, "SELECT status FROM invoice_lines WHERE id='inv-backup:1'")['status'],
                         'issued')
        l2 = one(self.db, "SELECT status,freeze_reason,amount FROM invoice_lines WHERE id='inv-backup:2'")
        self.assertEqual((l2['status'], l2['freeze_reason'], l2['amount']),
                         ('review', 'provenance_changed', 36))
        self.assertEqual(one(self.db, "SELECT status FROM settlements WHERE id='set-backup'")['status'],
                         'review')

        # strict invoice: only the amount-changed 50..52 line freezes
        self.assertEqual(one(self.db, "SELECT status FROM invoice_lines WHERE id='inv-strict:1'")['status'],
                         'issued')
        s2 = one(self.db, "SELECT status,freeze_reason FROM invoice_lines WHERE id='inv-strict:2'")
        self.assertEqual((s2['status'], s2['freeze_reason']), ('review', 'amount_changed'))

        # replacement credential re-selects the surviving 50 candidate
        recount = one(self.db, "SELECT handoff,status FROM calibrations "
                               "WHERE id='main-v2:cal:case-backup'")
        self.assertEqual(recount['handoff'], 50)
        self.assertEqual(recount['status'], 'active')

        # settlement threshold holds while the line is frozen ...
        again = self.ev('bill', {'op': 'settle', 'id': 'set-b-try',
                                 'invoice': 'inv-backup', 'tenant': 't', 'at': 72})
        self.assertEqual(again['decision'], 'invoice_blocked')
        # ... reconfirm the equal-amount provenance move ...
        rc = self.ev('bill', {'op': 'reconfirm', 'id': 'rc-1',
                              'line': 'inv-backup:2', 'tenant': 't'})
        self.assertEqual(rc['decision'], 'line_reconfirmed')
        self.assertEqual(one(self.db, "SELECT status FROM invoice_lines WHERE id='inv-backup:2'")['status'],
                         'issued')
        # ... and the invoice settles at the same balance
        settled2 = self.ev('bill', {'op': 'settle', 'id': 'set-backup-2',
                                     'invoice': 'inv-backup', 'tenant': 't', 'at': 73})
        self.assertEqual((settled2['decision'], settled2['total']), ('invoice_settled', 50))

        # 6) balances and seal/freeze state replay from the same event set
        result = replay(self.path)
        self.assertTrue(result['verified'], result['divergences'])

    # ---- identity / generation binding ------------------------------------

    def test_new_uid_and_generation_cannot_inherit_calibration(self):
        self.tariff_and_identity()
        self.probe_samples(swapped=None)
        good = self.ev('meter', {'op': 'calibrate', 'id': 'cal-good', 'tenant': 't',
                                 'identity': 'pod', 'uid': 'uid-a', 'resource': 'cpu',
                                 'primary': 'main', 'backup': 'bak-equal',
                                 'handoffs': [50, 52]})
        self.assertEqual(good['decision'], 'calibration_verified')

        # collection generation moves on while uid stays the same
        self.ev('meter', {'op': 'identity', 'id': 'pod', 'namespace': 'checkout',
                          'uid': 'uid-a', 'tenant': 't', 'generation': 2, 'cluster': 'east'})
        gen = self.ev('meter', {'op': 'seal', 'id': 'case-gen', 'identity': 'pod',
                                'sample': 'main', 'resource': 'cpu',
                                'start': 40, 'ends': 60,
                                'authorization': 'backup', 'calibration': 'cal-good'})
        self.assertFalse(gen['complete'])
        self.assertEqual(gen['denial'], 'generation_mismatch')

        # same workload name, new UID: old calibration must not carry over
        self.ev('meter', {'op': 'identity', 'id': 'pod', 'namespace': 'checkout',
                          'uid': 'uid-b', 'tenant': 't', 'generation': 3, 'cluster': 'east'})
        self.ev('meter', {'op': 'sample', 'id': 'main-b', 'identity': 'pod', 'uid': 'uid-b',
                          'tenant': 't', 'resource': 'cpu', 'cluster': 'east',
                          'start': 40, 'ends': 52, 'units': MAIN, 'source': 'node-meter'})
        uid = self.ev('meter', {'op': 'seal', 'id': 'case-uid', 'identity': 'pod',
                                'sample': 'main-b', 'resource': 'cpu',
                                'start': 40, 'ends': 60,
                                'authorization': 'backup', 'calibration': 'cal-good'})
        self.assertFalse(uid['complete'])
        self.assertEqual(uid['denial'], 'calibration_key_mismatch')
        self.assertEqual(uid['gaps'], [{'start': 52, 'ends': 60}])

        # calibrating the new uid against the old primary is refused outright
        denied = self.ev('meter', {'op': 'calibrate', 'id': 'cal-cross', 'tenant': 't',
                                   'identity': 'pod', 'uid': 'uid-b', 'resource': 'cpu',
                                   'primary': 'main', 'backup': 'bak-equal'})
        self.assertEqual(denied['decision'], 'calibration_denied')

    # ---- evidence-group selection -----------------------------------------

    def test_multiple_evidence_groups_select_farthest_complete_route(self):
        self.tariff_and_identity()
        self.probe_samples()
        # shorter calibrated backup only reaches 55
        short = [0, 0, 1, 0, 1, 2, 2] + [1, 1, 1]
        self.ev('meter', {'op': 'sample', 'id': 'bak-short', 'identity': 'pod',
                          'uid': 'uid-a', 'tenant': 't', 'resource': 'cpu', 'cluster': 'east',
                          'start': 45, 'ends': 55, 'units': short, 'source': 'sidecar'})
        pick = self.ev('meter', {'op': 'calibrate', 'id': 'cal-multi', 'tenant': 't',
                                 'identity': 'pod', 'uid': 'uid-a', 'resource': 'cpu',
                                 'primary': 'main',
                                 'candidates': [{'backup': 'bak-swapped', 'handoffs': [50, 52]},
                                                {'backup': 'bak-short', 'handoffs': [50, 52]},
                                                {'backup': 'bak-equal', 'handoffs': [50, 52]}]})
        self.assertEqual(pick['decision'], 'calibration_verified')
        self.assertEqual(pick['chosen']['backup'], 'bak-equal')  # reaches 60, farthest
        # rejected route evidence retained with reasons
        rejected = rows(self.db, "SELECT DISTINCT reason FROM calibration_routes "
                                 "WHERE calibration='cal-multi' AND verdict='rejected'")
        self.assertTrue(any(r['reason'] == 'usage_mismatch' for r in rejected))
        chosen_rows = rows(self.db, "SELECT * FROM calibration_routes "
                                   "WHERE calibration='cal-multi' AND verdict='chosen'")
        self.assertTrue(chosen_rows)

    # ---- raw trajectory encoding tolerance --------------------------------

    def test_explicit_chunk_trajectories_prove_segments(self):
        self.tariff_and_identity()
        self.ev('meter', {'op': 'identity', 'id': 'pod2', 'namespace': 'checkout',
                          'uid': 'uid-c', 'tenant': 't', 'generation': 1, 'cluster': 'east'})
        main_chunks = [[40, 45, 5], [45, 50, 2], [50, 52, 4]]
        good_chunks = [[45, 50, 2], [50, 52, 4], [52, 60, 8]]
        swapped_chunks = [[45, 50, 4], [50, 52, 2], [52, 60, 8]]
        self.ev('meter', {'op': 'sample', 'id': 'm', 'identity': 'pod2', 'uid': 'uid-c',
                          'tenant': 't', 'resource': 'cpu', 'cluster': 'east',
                          'start': 40, 'ends': 52, 'trajectory': main_chunks, 'source': 'node'})
        self.ev('meter', {'op': 'sample', 'id': 'b-bad', 'identity': 'pod2', 'uid': 'uid-c',
                          'tenant': 't', 'resource': 'cpu', 'cluster': 'east',
                          'start': 45, 'ends': 60, 'trajectory': swapped_chunks, 'source': 'bak'})
        bad = self.ev('meter', {'op': 'calibrate', 'id': 'c-bad', 'tenant': 't',
                                'identity': 'pod2', 'uid': 'uid-c', 'resource': 'cpu',
                                'primary': 'm', 'backup': 'b-bad', 'handoff': 52})
        self.assertEqual(bad['decision'], 'calibration_mismatch')

        self.ev('meter', {'op': 'sample', 'id': 'b-ok', 'identity': 'pod2', 'uid': 'uid-c',
                          'tenant': 't', 'resource': 'cpu', 'cluster': 'east',
                          'start': 45, 'ends': 60, 'segments': good_chunks, 'source': 'bak'})
        ok = self.ev('meter', {'op': 'calibrate', 'id': 'c-ok', 'tenant': 't',
                               'identity': 'pod2', 'uid': 'uid-c', 'resource': 'cpu',
                               'primary': 'm', 'backup': 'b-ok', 'handoff': 52})
        self.assertEqual(ok['decision'], 'calibration_verified')
        seal = self.ev('meter', {'op': 'seal', 'id': 'case-c', 'identity': 'pod2',
                                 'sample': 'm', 'resource': 'cpu', 'start': 40, 'ends': 60,
                                 'mode': 'backup', 'calibration': 'c-ok'})
        self.assertTrue(seal['complete'])
        issue = self.ev('bill', {'op': 'issue', 'invoice': 'inv-c', 'case': 'case-c', 'tenant': 't'})
        self.assertEqual(issue['total'], 50)

    def test_inline_segment_claims_and_auto_handoff_selection(self):
        # The 2/4 vs 4/2 proof can also be supplied directly on calibrate.
        self.tariff_and_identity()
        for sid in ('m', 'b'):
            start, ends = (40, 52) if sid == 'm' else (45, 60)
            self.ev('meter', {'op': 'sample', 'id': sid, 'identity': 'pod', 'uid': 'uid-a',
                              'tenant': 't', 'resource': 'cpu', 'cluster': 'east',
                              'start': start, 'ends': ends, 'units': 9, 'source': 'probe'})
        bad = self.ev('meter', {'op': 'calibration', 'id': 'c-bad', 'tenant': 't',
                                'identity': 'pod', 'uid': 'uid-a', 'resource': 'cpu',
                                'primary': 'm', 'backup': 'b',
                                'primary_segments': [2, 4], 'backup_segments': [4, 2]})
        self.assertEqual(bad['decision'], 'calibration_mismatch')
        good = self.ev('meter', {'op': 'calibration', 'id': 'c-good', 'tenant': 't',
                                 'identity': 'pod', 'uid': 'uid-a', 'resource': 'cpu',
                                 'primary': 'm', 'backup': 'b',
                                 'segments_a': [2, 4], 'segments_b': [2, 4]})
        self.assertEqual(good['decision'], 'calibration_verified')
        self.assertEqual(good['chosen']['handoff'], 52)  # auto-selected farthest candidate

    def test_legacy_single_sample_flow_unchanged(self):
        # scalar units still mean units-per-time and keep legacy decisions
        self.tariff_and_identity()
        self.ev('meter', {'op': 'sample', 'id': 'sample-a', 'identity': 'pod', 'uid': 'uid-a',
                          'tenant': 't', 'resource': 'cpu', 'cluster': 'east',
                          'start': 40, 'ends': 60, 'units': 2, 'source': 'node-meter'})
        gap = self.ev('meter', {'op': 'seal', 'id': 'gap-case', 'identity': 'pod',
                                'sample': 'sample-a', 'resource': 'cpu', 'start': 30, 'ends': 60})
        self.assertEqual(gap['decision'], 'evidence_gap')
        sealed = self.ev('meter', {'op': 'seal', 'id': 'meter-a', 'identity': 'pod',
                                    'sample': 'sample-a', 'resource': 'cpu',
                                    'start': 40, 'ends': 60})
        self.assertEqual(sealed, {'decision': 'meter_sealed', 'quantity': 40})
        issued = self.ev('bill', {'op': 'issue', 'invoice': 'invoice-a',
                                  'case': 'meter-a', 'tenant': 't'})
        self.assertEqual((issued['lines'], issued['total']), (2, 100))
        amounts = [l['amount'] for l in rows(self.db, "SELECT amount FROM invoice_lines "
                                                      "WHERE invoice='invoice-a' ORDER BY start")]
        self.assertEqual(amounts, [40, 60])


if __name__ == '__main__':
    unittest.main()
