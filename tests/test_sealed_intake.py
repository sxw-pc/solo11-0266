import json
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from usagecourt.sealed_intake import worlds
from usagecourt.store import connect, one, replay, run

ROOT = Path(__file__).resolve().parents[1]


def measured(frame='F1', left=2, right=10, token='slot-F1', serials=('A', 'B')):
    return dict(op='frame_measure', frame=frame, serials=list(serials),
                left_mg=left, right_mg=right, token=token, origin='line-2')


class SealedIntakeTests(unittest.TestCase):
    def setUp(self):
        self.work = TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.path = Path(self.work.name) / 'sealed.sqlite'
        self.db = connect(str(self.path))
        self.addCleanup(self.db.close)
        self.seq = 0

    def apply(self, *commands):
        results = []
        for index, command in enumerate(commands, 1):
            command = dict(command, event_id=command.get('event_id', f'ev-{self.seq}'))
            self.seq += 1
            results.append(run(self.db, 'sealed', command))
        return results

    def test_worlds_are_two_swaps_never_a_cross_product(self):
        pair = worlds(['A', 'B'], 2, 10)
        self.assertEqual(pair, [{'A': 2, 'B': 10}, {'A': 10, 'B': 2}])
        self.assertNotIn({'A': 10, 'B': 10}, pair)
        self.assertEqual([sum(w.values()) for w in pair], [12, 12])

    def test_batch_authorisation_receives_whole_frame_without_item_claims(self):
        self.assertEqual(self.apply(measured())[0]['total_mg'], 12)
        result = self.apply(dict(op='batch_authorize', frame='F1', receipt='R1',
                                 authority='materials', capacity_mg=12))[0]
        self.assertEqual(result['decision'], 'batch_received')
        self.assertEqual(result['serials'], ['A', 'B'])
        self.assertEqual(result['claimable'], [])
        self.assertEqual(result['total_mg'], 12)
        custody = self.apply(dict(op='sealed_custody', frame='F1'))[0]
        self.assertFalse(custody['claimable'])
        self.assertEqual(custody['holder'], 'repository')
        self.assertEqual(one(self.db, 'SELECT location FROM sealed_units '
                                    "WHERE frame='F1' AND serial='A'")['location'], 'sealed')
        for serial in ('A', 'B'):
            claim = self.apply(dict(op='claim', frame='F1', serial=serial))[0]
            self.assertEqual(claim['decision'], 'not_claimable')

    def test_eleven_milligram_bench_rejects_the_same_frame(self):
        self.apply(measured(left=10, right=10, serials=('A', 'B')))
        rejected = self.apply(dict(op='batch_authorize', frame='F1', receipt='R1',
                                   authority='materials', capacity_mg=11))[0]
        self.assertEqual(rejected['decision'], 'batch_capacity_exceeded')
        self.assertEqual(rejected['total_mg'], 20)
        # The uncertain [2, 10] frame is also 12 mg total: an 11 mg bench
        # cannot be made to admit it by picking a favourable wiring.
        self.db.execute("DELETE FROM sealed_frames")
        self.apply(measured())
        self.assertEqual(self.apply(dict(op='batch_authorize', frame='F1',
                                         receipt='R2', authority='materials',
                                         capacity_mg=11))[0]['decision'],
                         'batch_capacity_exceeded')

    def test_sort_contract_releases_only_after_real_direction_evidence(self):
        self.apply(measured())
        pending = self.apply(dict(op='sort_release', frame='F1', token='slot-F1'))[0]
        self.assertEqual(pending['decision'], 'direction_pending')
        self.assertEqual(pending['claimable'], [])
        self.assertEqual(one(self.db, 'SELECT COUNT(*) n FROM frame_receipts')['n'], 0)

        bad_token = self.apply(dict(op='direction_evidence', frame='F1', column='left',
                                    serial='A', token='other-slot'))[0]
        self.assertEqual(bad_token['decision'], 'handoff_token_mismatch')

        recorded = self.apply(
            dict(op='direction_evidence', frame='F1', column='left', serial='A',
                 token='slot-F1'))[0]
        self.assertEqual(recorded['decision'], 'direction_evidence_recorded')
        self.assertEqual(recorded['possible_worlds'], [{'A': 2, 'B': 10}])
        released = self.apply(dict(op='sort_release', frame='F1', token='slot-F1'))[0]
        self.assertEqual(released['decision'], 'sort_released')
        self.assertEqual(released['released'], ['A'])
        self.assertEqual(released['needs_wash'], ['B'])
        self.assertEqual(released['received_stock'], [])
        # A is claimable; B is still on the wash bench and never claimable.
        self.assertEqual(self.apply(dict(op='claim', frame='F1', serial='A'))[0]['decision'],
                         'item_claimable')
        self.assertEqual(self.apply(dict(op='claim', frame='F1', serial='B'))[0]['decision'],
                         'not_claimable')
        self.assertEqual(one(self.db, 'SELECT COUNT(*) n FROM frame_receipts')['n'], 0)
        self.assertFalse(self.apply(dict(op='batch_authorize', frame='F1', receipt='R9',
                                         authority='materials', capacity_mg=12))[0]
                         .get('decision') == 'batch_received')

    def test_reversed_wiring_does_not_sign_the_ten_milligram_item(self):
        # Real world A=10, B=2.  Evidence must say so; a favourable guess of
        # A=2 is not allowed just because the raw left column read 2.
        self.apply(measured())
        wrong = self.apply(dict(op='direction_evidence', frame='F1', column='left',
                                serial='B', token='slot-F1'))[0]
        self.assertEqual(wrong['decision'], 'direction_evidence_recorded')
        self.assertEqual(wrong['possible_worlds'], [{'A': 10, 'B': 2}])
        released = self.apply(dict(op='sort_release', frame='F1', token='slot-F1'))[0]
        self.assertEqual(released['released'], ['B'])
        self.assertEqual(self.apply(dict(op='claim', frame='F1', serial='B'))[0]['decision'],
                         'item_claimable')
        # A is 10 mg in the real world; it must not become claimable stock.
        self.assertEqual(self.apply(dict(op='claim', frame='F1', serial='A'))[0]['decision'],
                         'not_claimable')

    def test_partial_direction_is_pending_and_remeasure_clears_it(self):
        # One column of evidence still leaves both wirings possible, so no
        # item is released and none may be claimed.
        self.apply(measured())
        partial = self.apply(dict(op='direction_evidence', frame='F1', column='left',
                                  serial='A', token='slot-F1'))[0]
        self.assertEqual(len(partial['possible_worlds']), 1)
        self.assertEqual(self.apply(dict(op='sort_release', frame='F1',
                                         token='slot-F1'))[0]['released'], ['A'])

        # A recalibrated reading invalidates the resolved wiring: B is now the
        # only candidate and A's earlier release proves nothing about it.
        self.db.execute("DELETE FROM sealed_units")
        self.db.execute('UPDATE sealed_frames SET state=?', ('assembled',))
        remeasured = self.apply(measured(left=12, right=4, token='slot-F2'))[0]
        self.assertEqual(remeasured['decision'], 'frame_remeasured')
        self.assertTrue(remeasured['direction_cleared'])
        self.assertEqual(self.apply(dict(op='sort_release', frame='F1',
                                         token='slot-F2'))[0]['decision'],
                         'direction_pending')
        self.assertEqual(self.apply(dict(op='claim', frame='F1', serial='A'))[0]['decision'],
                         'unit_unknown')

    def test_legacy_position_readback_conflicts_on_reuse(self):
        self.apply(measured(frame='F1', token='slot-yesterday', serials=('A', 'B')))
        self.apply(dict(op='queue_enqueue', queue='Q1', frame='F1', receipt='R-old',
                        token='slot-yesterday', position=4))
        today = self.apply(measured(frame='F2', token='slot-today', serials=('C', 'D')))[0]
        self.assertEqual(today['decision'], 'frame_measured')
        self.apply(dict(op='queue_enqueue', queue='Q2', frame='F2', receipt='R-new',
                        token='slot-today', position=4))

        first = self.apply(dict(op='legacy_readback', queue='Q1', position=4,
                                raw='position 4 received', token='slot-yesterday',
                                origin='line-2'))[0]
        self.assertEqual(first['decision'], 'readback_attributed')
        self.assertEqual(first['holder'], 'F1')

        reused = self.apply(dict(op='frame_departs', frame='F1'))[0]
        self.assertEqual(reused['decision'], 'frame_departed')
        stale = self.apply(dict(op='queue_deliver', queue='Q1'))[0]
        self.assertEqual(stale['decision'], 'queue_receipt_stale')

        bare = self.apply(dict(op='legacy_readback', queue='Q2', position=4,
                               raw='position 4 received', token='slot-yesterday',
                               origin='line-2'))[0]
        self.assertEqual(bare['decision'], 'readback_unattributed')
        # Same raw text and same reusable position, but the holder stays the
        # origin because only yesterday's token was presented.
        self.assertEqual(bare['holder'], 'line-2')
        self.assertEqual(bare['claimable'], [])

    def test_queue_receipt_is_not_a_new_receipt_after_opening(self):
        self.apply(measured())
        self.apply(dict(op='queue_enqueue', queue='Q1', frame='F1', receipt='R1',
                        token='slot-F1', position=4))
        batch = self.apply(dict(op='batch_authorize', frame='F1', receipt='R1',
                                authority='materials', capacity_mg=12))[0]
        self.assertEqual(batch['decision'], 'batch_received')
        self.assertEqual(self.apply(dict(op='open_frame', frame='F1'))[0]['decision'],
                         'frame_opened')
        self.assertEqual(self.apply(dict(op='queue_deliver', queue='Q1'))[0]['decision'],
                         'queue_receipt_stale')

    def test_replay_rebuilds_sealed_state_from_events(self):
        self.apply(measured(), dict(op='batch_authorize', frame='F1', receipt='R1',
                                    authority='materials', capacity_mg=12))
        result = replay(str(self.path))
        self.assertTrue(result['verified'], result['divergences'])
        self.assertEqual(result['decisions'], 2)


class HandlingBoundaryTests(unittest.TestCase):
    def test_handling_forwards_sealed_ops_and_blocks_frame_serial_receipt(self):
        with closing(connect(':memory:')) as db:
            self.assertEqual(run(db, 'handling', dict(measured(), event_id='h1'))['decision'],
                             'frame_measured')
            result = run(db, 'handling', dict(op='receive', event_id='h2', frame='F1',
                                              serial='A', max_residue_mg=8))
            self.assertEqual(result['decision'], 'frame_direction_unresolved')
            self.assertEqual(result['claimable'], [])


if __name__ == '__main__':
    unittest.main()
