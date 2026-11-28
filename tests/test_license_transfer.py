"""Two-cluster licence-transfer tests using two independent databases.

Each test builds two real SQLite files, one per cluster agent.  Messages
cross between them by being re-submitted, never by sharing a transaction, so
these tests cannot pass by faking a distributed commit.  Every test also
replays both agents' logs into empty databases and compares the rebuilt tables.
"""
import json
import tempfile
import unittest
from pathlib import Path

from usagecourt import store

ROOT = Path(__file__).resolve().parents[1]
CRED = 'supplier-credential-1'
LICENSE = 'local-tools'
TENANT = 'tenant-a'
EAST_UID, WEST_UID = 'uid-east-1', 'uid-west-1'


class Cluster:
    """One agent's database plus a monotonically increasing event id."""

    def __init__(self, path, agent, cluster):
        self.path = Path(path)
        self.agent = agent
        self.cluster = cluster
        self.db = store.connect(str(self.path))
        self.calls = 0

    def close(self):
        self.db.close()

    def restart(self):
        self.db.close()
        self.db = store.connect(str(self.path))

    def event(self, op, **fields):
        self.calls += 1
        command = dict(fields, op=op, event_id='%s-%s-%d' % (self.cluster, op, self.calls))
        return store.run(self.db, 'license', command)

    def submit(self, event_id, op, **fields):
        """Deliver a message by its original id (as a retry would)."""
        return store.run(self.db, 'license', dict(fields, op=op, event_id=event_id))

    def replay(self):
        return store.replay(self.path)


class TwoClusterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.src = Cluster(Path(self.temp.name) / 'east.sqlite', 'agent-east', 'east')
        self.dst = Cluster(Path(self.temp.name) / 'west.sqlite', 'agent-west', 'west')
        self.addCleanup(self.src.close)
        self.addCleanup(self.dst.close)
        for site, uid in ((self.src, EAST_UID), (self.dst, WEST_UID)):
            store.run(site.db, 'meter', {
                'event_id': 'identity-' + site.cluster, 'op': 'identity', 'id': 'pod-' + site.cluster,
                'namespace': 'checkout', 'uid': uid, 'tenant': TENANT, 'generation': 1,
                'cluster': site.cluster})
            site.event('join', agent=site.agent, cluster=site.cluster)
            site.event('register', id=LICENSE, tenant=TENANT, product='compiler',
                       cluster=site.cluster, seats=1, origin='east')
        self.assertEqual(self.src.event('checkout', id='session-east', license=LICENSE,
                                        tenant=TENANT, identity='pod-east', uid=EAST_UID)
                         ['decision'], 'checked_out')

    # --- shared message helpers --------------------------------------------

    def approve(self, site, approval='approval-1', seat='session-east', target='west'):
        return site.event('approve', license=LICENSE, tenant=TENANT, source='east',
                          target=target, approval=approval, seat_id=seat, issued=10,
                          credential=CRED)

    def deliver(self, site, transfer='transfer-1', approval='approval-1'):
        return site.event('transfer', id=transfer, approval=approval, credential=CRED)

    def prove(self, site, evidence='proof-1', seat='session-east', uid=EAST_UID,
              boundary=60, restore='restore-60'):
        return site.event('stop_evidence', id=evidence, license=LICENSE, tenant=TENANT,
                          session=seat, uid=uid, boundary=boundary, restore_uid=restore,
                          credential=CRED)

    def stage(self, transfer='transfer-1', approval='approval-1', evidence='proof-1'):
        """Approve, deliver and record the stoppable point at both sites."""
        for site in (self.src, self.dst):
            self.approve(site, approval)
        self.deliver(self.src, transfer, approval)
        self.deliver(self.dst, transfer, approval)
        for site in (self.src, self.dst):
            self.prove(site, evidence)

    def arm(self, transfer='transfer-1', evidence='proof-1'):
        """The origin stops and signs; deliver the attestation to the destination."""
        armed = self.src.event('handoff', transfer=transfer, evidence=evidence, credential=CRED)
        self.assertEqual(armed['decision'], 'handoff_armed')
        delivered = self.dst.event('handoff', transfer=transfer, evidence=evidence,
                                   credential=CRED, attestation=armed['attestation'])
        self.assertEqual(delivered['decision'], 'handoff_armed')
        return armed

    def complete(self, attestation, transfer='transfer-1', evidence='proof-1',
                 session='session-west'):
        # The destination needs the origin's signed stop; callers pass the
        # attestation returned by arm().
        assert attestation
        return self.dst.event('complete', transfer=transfer, evidence=evidence,
                              session=session, identity='pod-west', uid=WEST_UID,
                              credential=CRED, attestation=attestation)

    def probe(self, site):
        return site.event('probe', license=LICENSE, tenant=TENANT)

    # --- assertions reused by several tests ---------------------------------

    def assert_no_second_seat(self):
        # Sessions opened at a site are that site's own rows; the sum is the
        # only honest two-site count (a session appears on exactly one site).
        seats = 0
        for site in (self.src, self.dst):
            seats += len(self.probe(site)['open_sessions'])
        self.assertEqual(seats, 1, 'exactly one seat may be open, found %d' % seats)


    def assert_replays(self):
        for site in (self.src, self.dst):
            result = site.replay()
            self.assertTrue(result['verified'], (site.cluster, result['divergences']))


class SeparateApprovalFromHandoffTests(TwoClusterTest):
    def test_approval_and_copy_grant_nothing_before_proof(self):
        self.approve(self.src)
        self.approve(self.dst)
        self.assertEqual(self.deliver(self.src)['decision'], 'transfer_delivered')
        self.assertEqual(self.deliver(self.dst)['decision'], 'transfer_delivered')
        # A copy is not a seat: the destination cannot issue its own.
        self.assertEqual(self.dst.event('checkout', id='session-west', license=LICENSE,
                                        tenant=TENANT, identity='pod-west', uid=WEST_UID)
                         ['decision'], 'not_issuer')
        # And it certainly cannot complete without the origin's signed stop.
        self.assertEqual(self.dst.event('complete', transfer='transfer-1', evidence='proof-1',
                                        session='session-west', identity='pod-west',
                                        uid=WEST_UID, credential=CRED)['decision'],
                         'evidence_required')
        self.assert_no_second_seat()

    def test_destination_cannot_arm_itself_from_the_copy_alone(self):
        self.stage()
        result = self.dst.event('handoff', transfer='transfer-1', evidence='proof-1',
                                credential=CRED)
        self.assertEqual(result['decision'], 'attestation_required')
        self.assertEqual(self.dst.event('complete', transfer='transfer-1', evidence='proof-1',
                                        session='session-west', identity='pod-west',
                                        uid=WEST_UID, credential=CRED)['decision'],
                         'attestation_required')

    def test_stale_restore_record_does_not_reopen_unarmed_session(self):
        self.stage()
        # The proof names a stoppable point, but the origin never acted on it.
        resumed = self.src.event('resume', license=LICENSE, tenant=TENANT,
                                 stopped_session='session-east', new_session='session-east-2',
                                 restore_uid='restore-60', uid=EAST_UID)
        # Nothing was stopped yet, so there is no closed session to resume.
        # The proof names a *possible* stop, not an actual one.
        self.assertEqual(resumed['decision'], 'unknown_session')
        self.assert_equal_open(self.src, ['session-east'])

    def test_same_name_new_uid_cannot_close_old_session(self):
        self.src.event('checkout', id='ignore', license=LICENSE, tenant=TENANT,
                       identity='pod-west', uid=WEST_UID)
        # A rebuilt east identity with a new UID reusing the same pod name.
        store.run(self.src.db, 'meter', {
            'event_id': 'identity-rebuild', 'op': 'identity', 'id': 'pod-east',
            'namespace': 'checkout', 'uid': 'uid-east-2', 'tenant': TENANT,
            'generation': 2, 'cluster': 'east'})
        result = self.src.event('checkpoint', session='session-east', license=LICENSE,
                                tenant=TENANT, uid='uid-east-2', boundary=61)
        self.assertEqual(result['decision'], 'uid_mismatch')
        # The original session, bound to the old UID, is still open.
        self.assert_equal_open(self.src, ['session-east'])

    def assert_equal_open(self, site, expected):
        probe = self.probe(site)
        self.assertEqual([s['id'] for s in probe['open_sessions']], expected)

    def test_licence_driven_checkpoint_can_be_continued_at_restore_point(self):
        result = self.src.event('checkpoint', license=LICENSE, tenant=TENANT,
                                session='session-east', uid=EAST_UID, boundary=50,
                                restore_uid='restore-50')
        self.assertEqual(result['decision'], 'session_checkpointed')
        # Wrong restore point is refused; the named one is legal.
        self.assertEqual(self.src.event('resume', license=LICENSE, tenant=TENANT,
                                        stopped_session='session-east',
                                        new_session='session-east-2',
                                        restore_uid='somewhere-else', uid=EAST_UID)
                         ['decision'], 'restore_point_required')
        resumed = self.src.event('resume', license=LICENSE, tenant=TENANT,
                                 stopped_session='session-east', new_session='session-east-2',
                                 restore_uid='restore-50', uid=EAST_UID)
        self.assertEqual(resumed['decision'], 'session_resumed')
        self.assertEqual((resumed['boundary'], resumed['uid']), (50, EAST_UID))
        self.assert_equal_open(self.src, ['session-east-2'])
        self.assert_replays()


class StayAtSourceTests(TwoClusterTest):
    def test_same_proof_keeps_session_running_and_transfer_incomplete(self):
        self.stage()
        deferred = self.src.event('leave', approval='approval-1', session='session-east',
                                  credential=CRED)
        self.assertEqual(deferred['decision'], 'transfer_deferred')
        self.assertEqual((deferred['authority'], deferred['issuer']), ('source', 'east'))
        self.assertTrue(deferred['may_issue'])
        # The destination recorded the same deferral and still holds no seat.
        self.assertEqual(self.dst.event('leave', approval='approval-1',
                                        credential=CRED)['decision'], 'transfer_deferred')
        self.assertEqual(self.dst.event('checkout', id='session-west', license=LICENSE,
                                        tenant=TENANT, identity='pod-west', uid=WEST_UID)
                         ['decision'], 'not_issuer')
        self.assertEqual(self.dst.event('complete', transfer='transfer-1', evidence='proof-1',
                                        session='session-west', identity='pod-west',
                                        uid=WEST_UID, credential=CRED)['decision'],
                         'transfer_deferred')
        # The original session is untouched and remains the only live seat.
        source = self.probe(self.src)
        self.assertEqual(source['authority'], 'source')
        self.assertEqual([s['id'] for s in source['open_sessions']], ['session-east'])
        self.assertEqual(source['stopped'], [])
        self.assert_no_second_seat()
        self.assert_replays()

    def test_stay_then_later_handoff_is_refused(self):
        self.stage()
        self.src.event('leave', approval='approval-1', session='session-east', credential=CRED)
        self.assertEqual(self.src.event('handoff', transfer='transfer-1', evidence='proof-1',
                                        credential=CRED)['decision'], 'transfer_revoked')


class MoveToDestinationTests(TwoClusterTest):
    def test_same_proof_moves_the_only_seat_to_the_destination(self):
        self.stage()
        armed = self.arm()
        # Source cannot reopen once armed, even before the destination confirms.
        self.assertEqual(self.src.event('checkout', id='session-east-2', license=LICENSE,
                                        tenant=TENANT, identity='pod-east', uid=EAST_UID)
                         ['decision'], 'not_issuer')
        self.assertEqual(self.src.event('resume', license=LICENSE, tenant=TENANT,
                                        stopped_session='session-east',
                                        new_session='session-east-2', restore_uid='restore-60',
                                        uid=EAST_UID)['decision'], 'handoff_pending')
        completed = self.complete(attestation=armed['attestation'])
        self.assertEqual(completed['decision'], 'seat_transferred')
        self.assertEqual((completed['cluster'], completed['uid']), ('west', WEST_UID))
        self.assertEqual((completed['source'], completed['stopped_session']),
                         ('east', 'session-east'))
        # The receipt names the new session; the origin acknowledges the stop.
        confirmed = self.src.event('complete', transfer='transfer-1', evidence='proof-1',
                                   session='session-west', credential=CRED,
                                   receipt=completed['receipt'])
        self.assertEqual(confirmed['decision'], 'handoff_confirmed')
        self.assertEqual((confirmed['authority'], confirmed['issuer']), ('target', 'west'))
        # One seat in total, now owned by the destination.
        self.assert_no_second_seat()
        self.assertEqual(self.probe(self.src)['may_issue'], False)
        self.assertEqual(self.probe(self.dst)['may_issue'], True)
        self.assert_replays()

    def test_source_retry_while_armed_cannot_reopen(self):
        self.stage()
        armed = self.arm()
        # The origin re-receives the handoff it already sent: a replay, not a reset.
        replay = self.src.event('handoff', transfer='transfer-1', evidence='proof-1',
                                credential=CRED)
        self.assertEqual(replay['decision'], 'handoff_armed')
        self.assertTrue(replay['replay'])
        self.assertEqual(self.src.event('checkout', id='session-east-2', license=LICENSE,
                                        tenant=TENANT, identity='pod-east', uid=EAST_UID)
                         ['decision'], 'not_issuer')

    def test_fingerprint_boundaries(self):
        self.stage()
        armed = self.arm()
        self.assertEqual(self.dst.event('complete', transfer='transfer-1', evidence='proof-1',
                                        session='session-west', identity='pod-west',
                                        uid='uid-west-2', credential=CRED,
                                        attestation=armed['attestation'])['decision'],
                         'not_entitled')
        self.assertEqual(self.dst.event('complete', transfer='transfer-1', evidence='proof-1',
                                        session='session-west', identity='pod-east',
                                        uid=EAST_UID, credential=CRED,
                                        attestation=armed['attestation'])['decision'],
                         'not_entitled')


class OrderingTests(TwoClusterTest):
    def test_remote_handoff_lost_before_destination_completes(self):
        self.stage()
        armed = self.arm()
        # The origin's acknowledgement to the destination never arrives: the
        # destination completes from its local copy + attestation anyway.
        completed = self.complete(attestation=armed['attestation'])
        self.assertEqual(completed['decision'], 'seat_transferred')
        # Source local state stays armed and blocked; it cannot reopen from
        # its own copy of the stopped session.
        self.assertEqual(self.src.event('checkout', id='session-east-2', license=LICENSE,
                                        tenant=TENANT, identity='pod-east', uid=EAST_UID)
                         ['decision'], 'not_issuer')
        self.assertEqual(self.src.event('resume', license=LICENSE, tenant=TENANT,
                                        stopped_session='session-east',
                                        new_session='session-east-2', restore_uid='restore-60',
                                        uid=EAST_UID)['decision'], 'handoff_pending')

    def test_source_restart_before_its_acknowledgement(self):
        self.stage()
        armed = self.arm()
        self.src.restart()
        replay = self.src.event('handoff', transfer='transfer-1', evidence='proof-1',
                                credential=CRED)
        self.assertEqual((replay['decision'], replay['replay']), ('handoff_armed', True))
        completed = self.complete(attestation=armed['attestation'])
        self.assertEqual(completed['decision'], 'seat_transferred')
        self.src.restart()
        confirmed = self.src.event('complete', transfer='transfer-1', evidence='proof-1',
                                   session='session-west', credential=CRED,
                                   receipt=completed['receipt'])
        self.assertEqual(confirmed['decision'], 'handoff_confirmed')
        self.assertEqual(self.probe(self.src)['issuer'], 'west')
        self.assert_replays()

    def test_late_revocation_cannot_revive_a_visible_seat(self):
        self.stage()
        armed = self.arm()
        completed = self.complete(attestation=armed['attestation'])
        self.assertEqual(completed['decision'], 'seat_transferred')
        late = self.src.event('revoke', transfer='transfer-1', credential=CRED)
        self.assertEqual(late['decision'], 'revoked_too_late')
        # Refused, and the source does not learn the destination's authority
        # from its own local rows: it stays blocked rather than reopening.
        self.assertEqual(late['restore_allowed'], False)
        self.assertEqual(late['next_issuer'], 'west')
        self.assertEqual(self.dst.event('revoke', transfer='transfer-1',
                                        credential=CRED)['decision'], 'revoked_too_late')
        self.assertEqual(self.src.event('resume', license=LICENSE, tenant=TENANT,
                                        stopped_session='session-east',
                                        new_session='session-east-2', restore_uid='restore-60',
                                        uid=EAST_UID)['decision'], 'handoff_pending')
        # The destination's live session survives untouched.
        self.assertEqual([s['uid'] for s in self.probe(self.dst)['open_sessions']], [WEST_UID])

    def test_withdrawal_before_arming_cancels_the_copy(self):
        self.stage()
        self.assertEqual(self.dst.event('revoke', transfer='transfer-1',
                                        credential=CRED)['decision'], 'transfer_withdrawn')
        self.assertEqual(self.dst.event('complete', transfer='transfer-1', evidence='proof-1',
                                        session='session-west', identity='pod-west',
                                        uid=WEST_UID, credential=CRED)['decision'],
                         'transfer_revoked')
        # Nothing was stopped at the origin, so its live session keeps running.
        self.assertEqual([s['id'] for s in self.probe(self.src)['open_sessions']],
                         ['session-east'])
        self.assert_no_second_seat()


class CopyDoesNotBlockSourceTests(TwoClusterTest):
    def test_source_may_keep_working_while_copy_is_delivered(self):
        self.stage()
        # The origin's live session continues while only a copy exists.
        self.assertTrue(self.probe(self.src)['may_issue'])
        self.assertEqual(self.probe(self.src)['open_sessions'][0]['id'], 'session-east')
        # The destination still may not issue from the copy alone.
        self.assertFalse(self.probe(self.dst)['may_issue'])


class OneProofTwoOutcomesTests(TwoClusterTest):
    """The same one-seat input supports two lawful results; only one applies."""

    def _stage_fresh(self):
        self.stage()

    def test_same_input_can_remain_at_source(self):
        self._stage_fresh()
        deferred = self.src.event('leave', approval='approval-1', session='session-east',
                                  credential=CRED)
        self.assertEqual(deferred['decision'], 'transfer_deferred')
        # The seat is usable only at the origin; the destination has no seat.
        self.assertTrue(self.probe(self.src)['may_issue'])
        self.assertEqual(self.probe(self.src)['issuer'], 'east')
        self.assertFalse(self.probe(self.dst)['may_issue'])
        self.assertEqual(self.probe(self.dst)['open_sessions'], [])

    def test_same_input_can_move_to_destination(self):
        self._stage_fresh()
        armed = self.arm()
        completed = self.complete(attestation=armed['attestation'])
        self.assertEqual(completed['decision'], 'seat_transferred')
        # The seat is usable only at the destination; the origin has none.
        self.assertFalse(self.probe(self.src)['may_issue'])
        self.assertEqual(self.probe(self.dst)['issuer'], 'west')
        self.assertTrue(self.probe(self.dst)['may_issue'])
        self.assertEqual(self.probe(self.src)['open_sessions'], [])

    def test_a_copy_is_never_counted_as_a_second_seat(self):
        self._stage_fresh()
        before = sum(len(self.probe(site)['open_sessions']) for site in (self.src, self.dst))
        self.assertEqual(before, 1)
        # Delivery itself opens nothing, twice.
        self.assertEqual(self.deliver(self.src)['decision'], 'transfer_delivered')
        self.assertEqual(self.deliver(self.dst)['decision'], 'transfer_delivered')
        after = sum(len(self.probe(site)['open_sessions']) for site in (self.src, self.dst))
        self.assertEqual(after, 1)


class ScopeAndGuardTests(TwoClusterTest):
    def test_transfer_requires_a_single_seat_licence(self):
        result = store.run(self.src.db, 'license', {
            'event_id': 'multi-register', 'op': 'register', 'id': 'multi', 'tenant': TENANT,
            'product': 'lab', 'cluster': 'east', 'seats': 2, 'origin': 'east'})
        self.assertEqual(result['decision'], 'license_registered')
        self.assertEqual(self.src.event('approve', license='multi', tenant=TENANT,
                                        source='east', target='west', approval='multi-approval',
                                        seat_id='session-east', issued=10,
                                        credential=CRED)['decision'],
                         'transfer_requires_single_seat')

    def test_origin_alone_cannot_mint_the_destinations_copy(self):
        # A second approval names a second copy; the origin may deliver it, but
        # the origin still cannot open the destination's seat from its own row.
        self.approve(self.src, approval='approval-2')
        self.approve(self.dst, approval='approval-2')
        self.assertEqual(self.deliver(self.src, transfer='transfer-2',
                                       approval='approval-2')['decision'], 'transfer_delivered')
        # No proof names transfer-2, so the origin cannot open anything from
        # its own delivered row; a missing receipt is never enough.
        self.assertEqual(self.src.event('complete', transfer='transfer-2',
                                        evidence='proof-1', session='session-east-2',
                                        credential=CRED)['decision'], 'evidence_required')
        self.assertEqual(self.probe(self.src)['open_sessions'][0]['id'], 'session-east')


class CredentialAndProbeTests(TwoClusterTest):
    def test_missing_supplier_credential_cancels_approval(self):
        self.assertEqual(self.src.event('approve', license=LICENSE, tenant=TENANT,
                                        source='east', target='west', approval='approval-1',
                                        seat_id='session-east', issued=10)['decision'],
                         'approval_unsigned')
        self.approve(self.src)
        self.approve(self.dst)
        self.assertEqual(self.deliver(self.src)['decision'], 'transfer_delivered')
        # A delivery signed with a different credential cannot match the copy.
        self.assertEqual(self.src.event('transfer', id='transfer-x', approval='approval-1',
                                        credential='wrong')['decision'], 'credential_mismatch')

    def test_probe_separates_restore_point_from_permission(self):
        self.stage()
        armed = self.arm()
        source = self.probe(self.src)
        self.assertEqual(source['restore_points'], ['restore-60'])
        self.assertEqual(source['resume_permitted'], False)
        self.assertEqual(source['armed_handoff'], 'west')
        self.assertEqual(source['next_issuer'], 'west')
        self.assertEqual(source['may_issue'], False)


if __name__ == '__main__':
    unittest.main()
