import json, unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from usagecourt import rotor, spinstand, workorder
from usagecourt.store import connect, replay, run, one, rows

ROOT = Path(__file__).resolve().parents[1]
PHASES = ['0', '90', '180', '270']
LIMITS = {'per_head_limit': 1, 'stand_limit': 1}


class WorkOrderTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = str(Path(temporary.name) / 'workorder.sqlite')
        self.db = connect(self.path)
        self.addCleanup(self.db.close)
        for row in json.loads((ROOT / 'fixtures/rotor_install.json').read_text()):
            self.assertIn(run(self.db, 'rotor', row)['decision'], ('stocked', 'installed'))
        self.sequence = 0

    def apply(self, subsystem, **data):
        self.sequence += 1
        return run(self.db, subsystem, dict(event_id=f't-{self.sequence}', **data))

    def mount(self, head, a, b):
        return self.apply('rotor', op='mount', head=head, a=a, b=b)

    def spin(self, heads, phase='270'):
        return self.apply('spinstand', op='spin', heads=heads, phase=phase,
                          individual_limit=1, stand_limit=1)

    def open_order(self, order_id, kind, simultaneous, same_pack, phases=PHASES):
        return self.apply('workorder', op='open_order', id=order_id, kind=kind,
                          simultaneous=simultaneous, uses_same_pack=same_pack,
                          heads=['A', 'B'], phases=phases, **LIMITS)

    def issue(self, order_id, tokens):
        return self.apply('workorder', op='issue', order=order_id, tokens=tokens)

    def accept_pair(self, order_id, phase):
        spin_result = self.spin(['A', 'B'], phase)
        accepted = self.apply('workorder', op='accept', order=order_id,
                              phase=phase, spin_event=spin_result['event_id'])
        return spin_result, accepted

    def accept_head(self, order_id, head):
        spin_result = self.spin([head])
        accepted = self.apply('workorder', op='accept', order=order_id,
                              head=head, spin_event=spin_result['event_id'])
        return spin_result, accepted

    # ---------- solver physics ----------

    def test_directed_responses_use_mounted_counts_and_phase_rotation(self):
        spec_a = rotor.head(self.db, 'A')['specification']
        spec_b = rotor.head(self.db, 'B')['specification']
        low = workorder.directed(spec_a, (1, 1), 'low')
        head_view = {'specification': spec_a, 'plane_a': ['w1'], 'plane_b': ['w2']}
        self.assertEqual(low, rotor.response(head_view, 'low'))
        self.assertEqual(workorder.directed(spec_a, (1, 1), 'high'), [0, -1])
        combined = workorder.paired_response(spec_a, spec_b, (1, 1), (1, 1), 'low', '270')
        manual = spinstand.combined(low, workorder.directed(spec_b, (1, 1), 'low'), '270')
        self.assertEqual(combined['base'], manual)
        self.assertEqual(combined['base_squared'], spinstand.squared(manual))
        # combined square is not the sum of individual squared magnitudes
        self.assertNotEqual(combined['base_squared'], sum(combined['individual_squared']))

    def test_joint_verification_covers_every_phase_and_speed(self):
        opened = self.open_order('joint', 'both_available', True, False)
        self.assertEqual(opened['decision'], 'workorder_planned')
        spec_a = rotor.head(self.db, 'A')['specification']
        spec_b = rotor.head(self.db, 'B')['specification']
        # the solver computes rotated vector sums, not sums of squared magnitudes
        non_orthogonal = workorder.paired_response(spec_a, spec_b, (1, 1), (0, 2),
                                                   'low', '0')
        self.assertNotEqual(non_orthogonal['base_squared'],
                            sum(non_orthogonal['individual_squared']))
        for phase in PHASES:
            for speed in rotor.SPEEDS:
                row = opened['plan']['verification']['paired'][phase][speed]
                self.assertLessEqual(row['base_squared'], 1)
                self.assertEqual(row['base_squared'], spinstand.squared(row['base']))
                self.assertEqual(row['base'], spinstand.combined(
                    row['readings'][0], row['readings'][1], phase))

    # ---------- rotation commission ----------

    def test_rotation_delivers_one_reusable_two_token_pack(self):
        opened = self.open_order('rotate', 'a_then_b', False, True)
        self.assertEqual((opened['decision'], opened['distinct_weights']),
                         ('workorder_planned', 2))
        tokens = opened['plan']['tokens']
        self.assertEqual(tokens, ['w1', 'w2'])
        self.assertEqual(self.issue('rotate', tokens)['decision'], 'tokens_issued')
        pack = opened['plan']['pack']

        self.mount('A', pack['a'], pack['b'])
        spin_a, accept_a = self.accept_head('rotate', 'A')
        self.assertEqual((spin_a['decision'], accept_a['decision']),
                         ('qualified', 'acceptance_recorded'))

        self.mount('A', [], [])
        self.mount('B', pack['a'], pack['b'])
        spin_b, accept_b = self.accept_head('rotate', 'B')
        self.assertEqual((spin_b['decision'], accept_b['decision']),
                         ('qualified', 'acceptance_recorded'))

        delivered = self.apply('workorder', op='deliver', order='rotate')
        self.assertEqual(delivered, {'decision': 'delivered', 'order': 'rotate',
                                     'mode': 'rotation', 'available_head': 'B',
                                     'simultaneous_capability': False,
                                     'tokens': tokens})
        head_b = rotor.head(self.db, 'B')
        self.assertEqual((head_b['plane_a'], head_b['plane_b']), (pack['a'], pack['b']))
        self.assertEqual(rotor.head(self.db, 'A')['plane_a'], [])
        self.assertTrue(replay(self.path)['verified'])

    def test_rotation_pack_keeps_plane_membership_across_swap(self):
        opened = self.open_order('rotate-plane', 'a_then_b', False, True)
        tokens = opened['plan']['tokens']
        self.issue('rotate-plane', tokens)
        pack = opened['plan']['pack']
        self.mount('A', pack['a'], pack['b'])
        self.accept_head('rotate-plane', 'A')
        self.mount('A', [], [])
        self.mount('B', pack['a'], pack['b'])
        self.accept_head('rotate-plane', 'B')
        head_b = rotor.head(self.db, 'B')
        self.assertEqual(head_b['plane_a'], pack['a'])
        self.assertEqual(head_b['plane_b'], pack['b'])

    def test_rotation_delivery_needs_both_acceptances(self):
        opened = self.open_order('rotate-half', 'a_then_b', False, True)
        self.issue('rotate-half', opened['plan']['tokens'])
        pack = opened['plan']['pack']
        self.mount('A', pack['a'], pack['b'])
        self.accept_head('rotate-half', 'A')
        blocked = self.apply('workorder', op='deliver', order='rotate-half')
        self.assertEqual(blocked, {'decision': 'acceptance_incomplete', 'missing': ['B']})

    # ---------- joint commission ----------

    def test_joint_needs_all_five_weights_and_passes_every_phase(self):
        opened = self.open_order('joint5', 'both_available', True, False)
        self.assertEqual((opened['decision'], opened['distinct_weights']),
                         ('workorder_planned', 5))
        tokens = opened['plan']['tokens']
        self.assertEqual(tokens, ['w1', 'w2', 'w3', 'w4', 'w5'])
        self.assertEqual(self.issue('joint5', tokens)['decision'], 'tokens_issued')
        packs = opened['plan']['packs']
        self.mount('A', packs['A']['a'], packs['A']['b'])
        self.mount('B', packs['B']['a'], packs['B']['b'])
        for phase in PHASES:
            spin_result, accepted = self.accept_pair('joint5', phase)
            self.assertEqual((spin_result['decision'], accepted['decision']),
                             ('qualified', 'acceptance_recorded'))
        delivered = self.apply('workorder', op='deliver', order='joint5')
        self.assertEqual(delivered['simultaneous_capability'], True)
        self.assertEqual(delivered['available_heads'], ['A', 'B'])
        self.assertTrue(replay(self.path)['verified'])

    def test_joint_refused_with_three_free_weights_carries_evidence(self):
        self.mount('A', ['w4', 'w5'], [])
        opened = self.open_order('joint4', 'both_available', True, False)
        self.assertEqual(opened['decision'], 'workorder_refused')
        evidence = opened['evidence']
        self.assertEqual(evidence['reason'], 'inventory_shortage')
        self.assertEqual(sorted(evidence['available']), ['w1', 'w2', 'w3'])
        self.assertEqual(evidence['required'], 5)
        four_token = [candidate for candidate in evidence['candidates']
                      if candidate['tokens'] <= 4]
        self.assertTrue(four_token)
        self.assertTrue(all(candidate['failures'] for candidate in four_token))
        first = four_token[0]['failures'][0]
        self.assertIn(first['scope'], ('base', 'individual'))
        self.assertGreater(first['squared'], first['limit'])
        self.assertEqual(one(self.db, "SELECT status FROM workorders WHERE id='joint4'")['status'],
                         'refused')

    def test_nominal_cancellation_in_one_phase_does_not_certify_others(self):
        spec_a = rotor.head(self.db, 'A')['specification']
        spec_b = rotor.head(self.db, 'B')['specification']
        powers = {phase: workorder.paired_response(
            spec_a, spec_b, (1, 1), (1, 1), 'low', phase)['base_squared']
            for phase in PHASES}
        self.assertEqual(min(powers.values()), 0)
        self.assertGreater(max(powers.values()), 1)
        # the solver never selects the nominal-only (1,1)+(1,1) arrangement
        opened = self.open_order('trap', 'both_available', True, False)
        self.assertEqual(opened['distinct_weights'], 5)
        for phase in PHASES:
            for speed in rotor.SPEEDS:
                self.assertLessEqual(
                    opened['plan']['verification']['paired'][phase][speed]['base_squared'], 1)

    def test_remount_after_acceptance_voids_old_shared_stand_receipt(self):
        opened = self.open_order('joint-stale', 'both_available', True, False)
        self.issue('joint-stale', opened['plan']['tokens'])
        packs = opened['plan']['packs']
        self.mount('A', packs['A']['a'], packs['A']['b'])
        self.mount('B', packs['B']['a'], packs['B']['b'])
        spin_270, accepted = self.accept_pair('joint-stale', '270')
        self.assertEqual(accepted['decision'], 'acceptance_recorded')
        old_revision = rotor.head(self.db, 'A')['revision']
        self.mount('A', packs['A']['a'], packs['A']['b'])
        self.assertGreater(rotor.head(self.db, 'A')['revision'], old_revision)
        # the old run is retained and still queryable ...
        self.assertIsNotNone(one(self.db, 'SELECT * FROM rotor_runs WHERE id=?',
                                 (spin_270['event_id'],)))
        # ... but it cannot certify the new revision
        stale = self.apply('workorder', op='accept', order='joint-stale',
                           phase='270', spin_event=spin_270['event_id'])
        self.assertEqual(stale['decision'], 'stale_revision')
        blocked = self.apply('workorder', op='deliver', order='joint-stale')
        self.assertEqual(blocked['decision'], 'acceptance_incomplete')
        for phase in PHASES:
            _, ok = self.accept_pair('joint-stale', phase)
            self.assertIn(ok['decision'], ('acceptance_recorded', 'acceptance_superseded'))
        self.assertEqual(
            self.apply('workorder', op='deliver', order='joint-stale')['decision'], 'delivered')
        # the stale run remains queryable alongside the superseding evidence
        self.assertIsNotNone(one(self.db, 'SELECT * FROM rotor_runs WHERE id=?',
                                 (spin_270['event_id'],)))

    def test_single_head_spin_cannot_back_joint_acceptance(self):
        opened = self.open_order('joint-solo', 'both_available', True, False)
        self.issue('joint-solo', opened['plan']['tokens'])
        packs = opened['plan']['packs']
        self.mount('A', packs['A']['a'], packs['A']['b'])
        self.mount('B', packs['B']['a'], packs['B']['b'])
        solo = self.spin(['A'])
        rejected = self.apply('workorder', op='accept', order='joint-solo',
                              phase='270', spin_event=solo['event_id'])
        self.assertEqual(rejected['decision'], 'spin_evidence_mismatch')

    # ---------- numbered claims ----------

    def test_duplicate_and_foreign_claims_are_blocked(self):
        opened = self.open_order('rotate-x', 'a_then_b', False, True)
        self.issue('rotate-x', opened['plan']['tokens'])
        other = self.open_order('rotate-y', 'a_then_b', False, True)
        self.assertEqual(other['decision'], 'workorder_planned')
        # the solver routes the second order around serials already held
        self.assertEqual(set(other['plan']['tokens']).intersection(opened['plan']['tokens']),
                         set())
        # explicitly asking for a serial held by another order is refused
        blocked = self.apply('workorder', op='issue', order='rotate-y',
                             tokens=opened['plan']['tokens'])
        self.assertEqual(blocked['decision'], 'token_already_claimed')
        self.assertEqual(self.issue('rotate-y', ['w3'])['decision'], 'tokens_mismatch')
        self.assertEqual(self.issue('rotate-y', ['w3', 'ghost'])['decision'], 'unknown_token')
        # no claims were created for the rejected attempts
        self.assertEqual(
            rows(self.db, "SELECT * FROM weight_claims WHERE order_id='rotate-y'"), [])

    def test_mounted_weight_cannot_be_claimed_until_released(self):
        self.mount('B', ['w1'], [])
        opened = self.open_order('rotate-z', 'a_then_b', False, True)
        # the solver skips the mounted serial and still finds a two-token pack
        self.assertEqual(opened['plan']['tokens'], ['w2', 'w3'])
        self.assertEqual(self.issue('rotate-z', ['w2', 'w3'])['decision'], 'tokens_issued')
        # once w1 is released, a new order may use it together with a free serial,
        # but never the serials still held by rotate-z
        self.mount('B', [], [])
        second = self.open_order('rotate-z2', 'a_then_b', False, True)
        self.assertEqual(second['plan']['tokens'], ['w1', 'w4'])
        self.assertEqual(self.issue('rotate-z2', ['w1', 'w4'])['decision'], 'tokens_issued')
        greedy = self.apply('workorder', op='issue', order='rotate-z2',
                            tokens=['w2', 'w1'])
        self.assertEqual(greedy['decision'], 'token_already_claimed')
        self.assertEqual(
            one(self.db, "SELECT COUNT(*) n FROM weight_claims WHERE order_id='rotate-z2'")['n'],
            2)

    def test_repeated_issue_event_is_idempotent(self):
        opened = self.open_order('rotate-idem', 'a_then_b', False, True)
        tokens = opened['plan']['tokens']
        command = {'event_id': 'issue-idem', 'op': 'issue', 'order': 'rotate-idem',
                   'tokens': tokens}
        first = run(self.db, 'workorder', dict(command))
        second = run(self.db, 'workorder', dict(command))
        self.assertEqual((first['decision'], second['decision']),
                         ('tokens_issued', 'tokens_issued'))
        self.assertEqual(len(rows(self.db,
                                  "SELECT * FROM weight_claims WHERE order_id='rotate-idem'")), 2)

    # ---------- interruption, replan, fixture, replay ----------

    def test_reopen_same_db_resumes_without_double_claims(self):
        opened = self.open_order('rotate-r', 'a_then_b', False, True)
        self.issue('rotate-r', opened['plan']['tokens'])
        self.db.close()
        reopened = connect(self.path)
        report = replay(self.path)
        self.assertTrue(report['verified'], report['divergences'])
        claims = rows(reopened, "SELECT * FROM weight_claims WHERE order_id='rotate-r'")
        self.assertEqual(len(claims), 2)
        self.assertEqual({claim['status'] for claim in claims}, {'held'})
        reopened.close()
        self.db = connect(self.path)

    def test_refused_order_replans_after_restock_on_same_order(self):
        self.mount('A', ['w4', 'w5'], [])
        opened = self.open_order('short', 'both_available', True, False)
        self.assertEqual(opened['decision'], 'workorder_refused')
        self.mount('A', [], [])
        replanned = self.apply('workorder', op='replan', order='short')
        self.assertEqual(replanned['decision'], 'workorder_planned')
        self.assertEqual(replanned['distinct_weights'], 5)
        self.assertEqual(one(self.db, "SELECT status FROM workorders WHERE id='short'")['status'],
                         'planned')

    def test_invalid_contract_shapes_are_rejected(self):
        bad = self.apply('workorder', op='open_order', id='bad', kind='x',
                         simultaneous=True, uses_same_pack=True,
                         heads=['A', 'B'], phases=PHASES, **LIMITS)
        self.assertEqual(bad['decision'], 'invalid_order')
        bad_phase = self.apply('workorder', op='open_order', id='bad2', kind='x',
                               simultaneous=True, uses_same_pack=False,
                               heads=['A', 'B'], phases=['45'], **LIMITS)
        self.assertEqual(bad_phase['decision'], 'invalid_order')

    def test_rotation_and_joint_fixtures_replay_clean(self):
        for name in ('workorder_rotation.json', 'workorder_joint.json',
                     'workorder_shortage.json'):
            with TemporaryDirectory() as directory:
                db_path = str(Path(directory) / name.replace('.json', '.sqlite'))
                fixture_db = connect(db_path)
                decisions = []
                for command in json.loads((ROOT / 'fixtures' / name).read_text()):
                    decisions.append(run(fixture_db,
                                         command.get('subsystem', 'workorder'),
                                         command)['decision'])
                fixture_db.close()
                self.assertNotIn('stale_revision', decisions)
                report = replay(db_path)
                self.assertTrue(report['verified'], report['divergences'])
                if name == 'workorder_shortage.json':
                    self.assertEqual(decisions.count('workorder_refused'), 1)
                self.assertEqual(decisions.count('delivered'), 1)


if __name__ == '__main__':
    unittest.main()
