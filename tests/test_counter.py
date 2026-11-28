import tempfile
import unittest
from pathlib import Path

from usagecourt import counter
from usagecourt.store import connect, run, one, rows, replay

SLOT = 'rack-7'
OLD, SPARE = 'accel-a', 'accel-b'
AUTH = 'swap-auth-1'


class CounterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'counter.sqlite'
        self.db = connect(str(self.path))
        self.addCleanup(self.db.close)
        self.agent = counter.DatacenterAgent()
        self.coord = counter.Coordinator()

    # helpers --------------------------------------------------------------- #
    def authorize(self, uid='uid-a', generation=1):
        return run(self.db, 'counter', {
            'event_id': 'auth-1', 'op': 'authorize', 'id': AUTH, 'slot': SLOT,
            'tenant': 'tenant-a', 'cluster': 'east', 'identity': 'pod-a',
            'uid': uid, 'generation': generation, 'at': 10,
            'old_device': OLD, 'backup_device': SPARE})

    def run_devices(self, old_value=98, spare_value=0, spare_blocked=True):
        self.a = self.agent.register(counter.Device(OLD, old_value))
        self.b = self.agent.register(counter.Device(SPARE, spare_value,
                                                    blocked=spare_blocked))
        return self.a, self.b

    def evidence(self, eid, device, kind, at, value=None, detail=None, cycle=None):
        return run(self.db, 'counter', {
            'event_id': 'ev-' + eid, 'op': 'intake', 'id': 'ev-' + eid,
            'authorization': AUTH, 'device': device, 'cycle': cycle,
            'kind': kind, 'at': at, 'value': value, 'source': self.coord.name,
            'detail': detail})

    def first_reading(self, device, eid, at, cycle=None):
        fact = self.coord.witness_read(device, at)
        return self.evidence(eid, device.name, 'witnessed_read', at,
                             value=fact['value'], cycle=cycle)

    def do_work(self, device, eid, at, units, cycle=None):
        """Drive the real device and lodge what the coordinator can prove."""
        device.work(units)
        fact = self.coord.witness_read(device, at)
        return self.evidence(eid, device.name, 'witnessed_read', at,
                             value=fact['value'], cycle=cycle)

    def do_stop(self, device, command_id, at, cycle=None):
        self.agent.enqueue(command_id, device.name, 'stop', at)
        self.agent.deliver(command_id)
        fact = self.coord.witness_stop(device, at)
        return self.evidence(command_id, device.name, 'witnessed_stop', at,
                             value=fact['value'], cycle=cycle)

    def do_zero(self, device, command_id, at, cycle=None, lost_reply=False):
        self.agent.enqueue(command_id, device.name, 'zero', at)
        self.agent.deliver(command_id, lost_reply=lost_reply)
        if not lost_reply:
            fact = self.coord.witness_zero(device, command_id, at)
            if fact:
                self.evidence(command_id, device.name, 'witnessed_zero', at,
                              value=0, detail=command_id, cycle=cycle)
    def open_cycle_id(self, device):
        return one(self.db, "SELECT id FROM counter_cycles WHERE device=? AND status='open'",
                   (device,))['id']

    # 1. same-name new UID: meter rejects stale packets, inspection intact --- #
    def test_same_name_new_uid_stale_packet_rejected_but_inspection_complete(self):
        # Fill inspection positions while the old UID owns the identity.
        run(self.db, 'meter', {'event_id': 'id-1', 'op': 'identity', 'id': 'pod-a',
             'namespace': 'checkout', 'uid': 'uid-a', 'tenant': 'tenant-a',
             'generation': 1, 'cluster': 'east'})
        run(self.db, 'meter', {'event_id': 's1', 'op': 'sample', 'id': 'sample-a',
             'identity': 'pod-a', 'uid': 'uid-a', 'tenant': 'tenant-a',
             'resource': 'cpu', 'cluster': 'east', 'start': 40, 'ends': 60,
             'units': 2, 'source': 'node-meter'})
        run(self.db, 'inspection', {'event_id': 'in-assign', 'op': 'assign',
             'id': 'east-observation', 'cluster': 'east', 'start': 0, 'end': 3})
        for pos in range(3):
            self.assertEqual(run(self.db, 'inspection', {
                'event_id': f'in-p{pos}', 'op': 'observe',
                'inspection': 'east-observation', 'position': pos,
                'sample': 'sample-a'})['decision'], 'position_recorded')
        # Platform rotates the service identity to a brand-new same-name UID.
        run(self.db, 'meter', {'event_id': 'id-2', 'op': 'identity', 'id': 'pod-a',
             'namespace': 'checkout', 'uid': 'uid-b', 'tenant': 'tenant-b',
             'generation': 2, 'cluster': 'east'})
        stale = run(self.db, 'meter', {'event_id': 'old-packet', 'op': 'sample',
             'id': 'old-packet', 'identity': 'pod-a', 'uid': 'uid-a',
             'tenant': 'tenant-a', 'resource': 'cpu', 'cluster': 'east',
             'start': 60, 'ends': 70, 'units': 2, 'source': 'node-meter'})
        self.assertEqual(stale['decision'], 'sample_denied')
        # Inspection only verifies what already exists; the packet set stays whole.
        report = run(self.db, 'inspection', {'event_id': 'in-final', 'op': 'inspect',
                      'inspection': 'east-observation'})
        self.assertEqual(report, {'decision': 'collection_complete', 'gaps': []})

        # The counter side enforces the same boundary at the room: an old-device
        # packet carrying the old UID cannot be booked under the new one.
        self.authorize(uid='uid-b', generation=2)
        self.run_devices()
        rejected = run(self.db, 'counter', {'event_id': 'stale-room-read',
             'op': 'reading', 'authorization': AUTH, 'device': OLD, 'uid': 'uid-a',
             'at': 70, 'value': 5, 'source': 'room-agent'})
        self.assertEqual(rejected['decision'], 'reading_rejected_stale')
        self.assertEqual(one(self.db, "SELECT state FROM counter_readings WHERE id='stale-room-read'")
                         ['state'], 'stale')
        self.assertTrue(replay(str(self.path))['verified'])

    # 2. route retain: old device keeps accepting, spare blocked, wrap sealed #
    def test_retain_route_seals_open_physical_period_spare_cannot_accept(self):
        self.authorize()
        a, b = self.run_devices()
        cycle = self.open_cycle_id(OLD)
        # Ordinary readings 98 then wrap to 02, plus one more 3-unit task to 05.
        self.first_reading(a, 'a-r98', 11, cycle)
        self.do_work(a, 'a-r02', 12, 4, cycle)    # 98+4 -> 02 (mod 100)
        self.do_work(a, 'a-r05', 13, 3, cycle)    # 02+3 -> 05
        self.assertEqual(a.read(), 5)
        # A previous-shift zero is durably queued in the room agent and may run later.
        self.agent.enqueue('cmd-zero-late', OLD, 'zero', 14)
        run(self.db, 'counter', {'event_id': 'q-zero', 'op': 'command',
            'authorization': AUTH, 'device': OLD, 'action': 'enqueue',
            'id': 'cmd-zero-late', 'kind': 'zero', 'at': 14})
        retained = run(self.db, 'counter', {'event_id': 'route-retain',
            'op': 'route', 'authorization': AUTH, 'slot': SLOT, 'choice': 'retain',
            'at': 15})
        self.assertEqual(retained['decision'], 'route_retained')
        # Physical boundary: spare cannot accept while the old machine still can.
        self.assertTrue(a.accepting)
        self.assertFalse(b.accepting)
        self.assertEqual(b.work(3), None)
        # Sealing the still-open period: wrap 98->02 is 4, then 02->05 is 3.
        seal = run(self.db, 'counter', {'event_id': 'seal-1', 'op': 'seal',
            'authorization': AUTH, 'device': OLD, 'ends': 13})
        self.assertEqual((seal['decision'], seal['units']), ('counter_cycle_sealed', 7))
        # But the pending zero forbids opening a further period on the device.
        self.assertEqual(seal['next_cycle'], 'blocked_pending_zero')
        self.assertEqual(run(self.db, 'counter', {'event_id': 'open-x', 'op': 'open_cycle',
            'authorization': AUTH, 'device': OLD, 'id': 'cyc-x', 'at': 16})['decision'],
            'counter_zero_pending')
        # Cancellation is legal only while still queued.
        self.assertEqual(self.agent.cancel('cmd-zero-late')['state'], 'cancelled')
        run(self.db, 'counter', {'event_id': 'cancel-zero', 'op': 'command',
            'authorization': AUTH, 'device': OLD, 'action': 'cancel',
            'id': 'cmd-zero-late', 'at': 16})
        # Sealed physical evidence remains usable for inspection after the later
        # UID rotation, yet creates no settlement record.
        run(self.db, 'counter', {'event_id': 'rot-2', 'op': 'rotate',
            'authorization': AUTH, 'slot': SLOT, 'uid': 'uid-b', 'generation': 2})
        sample = 'counter-sample:' + cycle
        self.assertEqual(one(self.db, 'SELECT uid,units,status FROM samples WHERE id=?',
                         (sample,))['uid'], 'uid-a')
        run(self.db, 'inspection', {'event_id': 'c-assign', 'op': 'assign',
            'id': 'counter-observation', 'cluster': 'east', 'start': 0, 'end': 1})
        self.assertEqual(run(self.db, 'inspection', {'event_id': 'c-observe',
            'op': 'observe', 'inspection': 'counter-observation', 'position': 0,
            'sample': sample})['decision'], 'position_recorded')
        self.assertEqual(rows(self.db, 'SELECT * FROM settlements'), [])
        self.assertTrue(replay(str(self.path))['verified'])

    # 3. route replace: stop old machine verifiably, spare opens new period -- #
    def test_replace_route_requires_verified_stop_and_spare_first_task_opens_period(self):
        self.authorize()
        a, b = self.run_devices()
        cycle = self.open_cycle_id(OLD)
        # Replacing before the old machine physically stops is rejected.
        self.assertEqual(run(self.db, 'counter', {'event_id': 'route-early',
            'op': 'route', 'authorization': AUTH, 'slot': SLOT,
            'choice': 'replace', 'at': 20})['decision'], 'physical_intake_not_closed')
        # Last ordinary reading 98 -> wrap 02 -> final task 05, then real stop.
        self.first_reading(a, 'a-r98', 11, cycle)
        self.do_work(a, 'a-r02', 12, 4, cycle)
        self.do_work(a, 'a-r05', 13, 3, cycle)
        self.do_stop(a, 'cmd-stop', 14, cycle)
        self.assertFalse(a.accepting)
        # Work between the last ordinary reading and the stop cannot land on the spare.
        self.assertFalse(b.accepting)
        replaced = run(self.db, 'counter', {'event_id': 'route-replace',
            'op': 'route', 'authorization': AUTH, 'slot': SLOT,
            'choice': 'replace', 'at': 15})
        self.assertEqual(replaced['decision'], 'route_replaced')
        self.assertEqual(replaced['active_device'], SPARE)
        # The route choice does not fabricate acceptance: simulate the real first task.
        b.enable()
        self.assertTrue(b.accepting)
        self.assertEqual(b.work(4), 4)
        spare_cycle = replaced['cycle']
        self.first_reading(b, 'b-r04', 16, spare_cycle)
        # Old period endpoint is verifiable: wrap 98->02 (4) then 02->05 (3).
        seal = run(self.db, 'counter', {'event_id': 'seal-old', 'op': 'seal',
            'authorization': AUTH, 'device': OLD, 'ends': 14})
        self.assertEqual((seal['decision'], seal['units']), ('counter_cycle_sealed', 7))
        # Old machine can no longer accept anything for the UID.
        self.assertEqual(a.work(9), None)
        self.assertEqual(run(self.db, 'counter', {'event_id': 'old-read', 'op': 'reading',
            'authorization': AUTH, 'device': OLD, 'uid': 'uid-a', 'at': 17,
            'value': 5})['decision'], 'reading_rejected_stale')
        fresh = run(self.db, 'counter', {'event_id': 'new-read', 'op': 'reading',
            'authorization': AUTH, 'device': SPARE, 'uid': 'uid-a', 'at': 16,
            'value': 4})
        self.assertEqual(fresh['decision'], 'reading_recorded')
        self.assertTrue(replay(str(self.path))['verified'])

    # 4. identical 05 bytes, good timeline: original evidence preserved ------ #
    def test_identical_bytes_sealable_when_coordinator_binds_physical_period(self):
        self.authorize()
        a, b = self.run_devices()
        # Both physical timelines emit the same central remainder sequence:
        # 98, 02, 05.  A wraps; B really zeroes between 02 and 05.
        cycle_a = self.open_cycle_id(OLD)
        self.first_reading(a, 'a-r98', 11, cycle_a)
        self.do_work(a, 'a-r02', 12, 4, cycle_a)
        self.do_work(a, 'a-r05a', 13, 3, cycle_a)
        # A previous-shift zero queued for A never ran; the cancellation is receipted.
        self.agent.enqueue('cmd-zero-a', OLD, 'zero', 14)
        self.assertEqual(self.agent.cancel('cmd-zero-a')['receipt'],
                         'cancel:cmd-zero-a')
        run(self.db, 'counter', {'event_id': 'qa', 'op': 'command',
            'authorization': AUTH, 'device': OLD, 'action': 'enqueue',
            'id': 'cmd-zero-a', 'kind': 'zero', 'at': 14})
        run(self.db, 'counter', {'event_id': 'ca', 'op': 'command',
            'authorization': AUTH, 'device': OLD, 'action': 'cancel',
            'id': 'cmd-zero-a', 'at': 14})
        seal = run(self.db, 'counter', {'event_id': 'seal-a', 'op': 'seal',
            'authorization': AUTH, 'device': OLD, 'ends': 13})
        self.assertEqual((seal['decision'], seal['units']), ('counter_cycle_sealed', 7))
        self.assertEqual(a.read(), 5)

        # Timeline B: stop A, commission B, then B zeroes for real and regrows to 05.
        self.do_stop(a, 'cmd-stop-a', 15)
        run(self.db, 'counter', {'event_id': 'route-b', 'op': 'route',
            'authorization': AUTH, 'slot': SLOT, 'choice': 'replace', 'at': 16})
        b.enable()
        spare_cycle = self.open_cycle_id(SPARE)
        self.first_reading(b, 'b-r98', 17, spare_cycle)
        self.do_work(b, 'b-r02', 18, 4, spare_cycle)
        self.do_zero(b, 'cmd-zero-b', 19, spare_cycle)
        self.do_work(b, 'b-r05', 20, 5, spare_cycle)
        self.assertEqual(b.read(), 5)
        # Seal only through the witnessed zero: the old physical period on B ends
        # at the reset.  Identical 05 byte, but it cannot be folded into it.
        seal_b = run(self.db, 'counter', {'event_id': 'seal-b', 'op': 'seal',
            'authorization': AUTH, 'device': SPARE, 'ends': 19})
        self.assertEqual(seal_b['decision'], 'counter_cycle_sealed')
        self.assertEqual(seal_b['units'], 4)  # wrap 98->02 only; post-zero 5 is new
        successor = self.open_cycle_id(SPARE)
        self.assertNotEqual(successor, spare_cycle)
        self.assertTrue(replay(str(self.path))['verified'])

    # 5. identical 05 bytes, bad timeline: no provable evidence --------------- #
    def test_ambiguous_remainder_cannot_be_sealed_or_reopen_device(self):
        self.authorize()
        a, b = self.run_devices(old_value=2)
        cycle = self.open_cycle_id(OLD)
        # The central library only ever saw 02 then 05; the room holds an
        # executed zero whose reply was lost, and the isolated entry restarted,
        # so it cannot prove which physical period the 05 belongs to.
        self.first_reading(a, 'a-r02', 12, cycle)
        self.agent.enqueue('cmd-zero-lost', OLD, 'zero', 13)
        self.agent.deliver('cmd-zero-lost', lost_reply=True)
        self.coord.note_restart(a, 13)
        self.evidence('a-restart', OLD, 'restart_gap', 13, cycle=cycle)
        a.work(3)
        self.first_reading(a, 'a-r05', 14, cycle)
        run(self.db, 'counter', {'event_id': 'q-lost', 'op': 'command',
            'authorization': AUTH, 'device': OLD, 'action': 'enqueue',
            'id': 'cmd-zero-lost', 'kind': 'zero', 'at': 13})
        run(self.db, 'counter', {'event_id': 'late-report', 'op': 'command',
            'authorization': AUTH, 'device': OLD, 'action': 'report',
            'id': 'cmd-zero-lost', 'at': 15})
        # The unconfirmed operation cannot be relabelled a cancellation.
        self.assertEqual(run(self.db, 'counter', {'event_id': 'bad-cancel',
            'op': 'command', 'authorization': AUTH, 'device': OLD,
            'action': 'cancel', 'id': 'cmd-zero-lost', 'at': 15})['decision'],
            'cancel_rejected_command_delivered')
        # Sealing the indistinguishable remainder is refused and the slot is in review.
        seal = run(self.db, 'counter', {'event_id': 'seal-bad', 'op': 'seal',
            'authorization': AUTH, 'device': OLD, 'ends': 14})
        self.assertEqual(seal['decision'], 'counter_remainder_ambiguous')
        # The device cannot be reopened for its next period, and the spare route
        # cannot be opened while the physical period is unresolved.
        self.assertEqual(run(self.db, 'counter', {'event_id': 'open-bad',
            'op': 'open_cycle', 'authorization': AUTH, 'device': OLD,
            'id': 'cyc-bad', 'at': 16})['decision'], 'counter_review_blocked')
        self.assertEqual(run(self.db, 'counter', {'event_id': 'replace-bad',
            'op': 'route', 'authorization': AUTH, 'slot': SLOT,
            'choice': 'replace', 'at': 16})['decision'], 'counter_review_blocked')
        self.assertEqual(one(self.db, "SELECT state FROM counter_authorizations WHERE id=?",
                         (AUTH,))['state'], 'review')
        # The entry only kept facts it could prove; no inspectable sample exists.
        self.assertEqual(rows(self.db, "SELECT id FROM samples WHERE source='coordinator-entry'"), [])
        self.assertTrue(replay(str(self.path))['verified'])

    # 7. lost stop reply: isolated entry proves physical closure anyway ------ #
    def test_lost_stop_reply_still_replaceable_when_entry_proves_closure(self):
        self.authorize()
        a, b = self.run_devices()
        cycle = self.open_cycle_id(OLD)
        self.first_reading(a, 'a-r98', 11, cycle)
        self.do_work(a, 'a-r02', 12, 4, cycle)
        self.do_work(a, 'a-r05', 13, 3, cycle)
        # Stop executes physically but its wire reply is lost.
        self.agent.enqueue('cmd-stop-lost', OLD, 'stop', 14)
        self.agent.deliver('cmd-stop-lost', lost_reply=True)
        run(self.db, 'counter', {'event_id': 'q-stop', 'op': 'command',
            'authorization': AUTH, 'device': OLD, 'action': 'enqueue',
            'id': 'cmd-stop-lost', 'kind': 'stop', 'at': 14})
        run(self.db, 'counter', {'event_id': 'r-stop', 'op': 'command',
            'authorization': AUTH, 'device': OLD, 'action': 'report',
            'id': 'cmd-stop-lost', 'at': 15})
        self.assertFalse(a.accepting)
        # Before the entry proves closure, the route is blocked.
        self.assertEqual(run(self.db, 'counter', {'event_id': 'route-blocked',
            'op': 'route', 'authorization': AUTH, 'slot': SLOT,
            'choice': 'replace', 'at': 15})['decision'], 'counter_stop_pending')
        # The isolated entry independently records the closed physical boundary.
        fact = self.coord.witness_stop(a, 15)
        self.evidence('a-stop', OLD, 'witnessed_stop', 15, value=fact['value'], cycle=cycle)
        replaced = run(self.db, 'counter', {'event_id': 'route-ok', 'op': 'route',
            'authorization': AUTH, 'slot': SLOT, 'choice': 'replace', 'at': 16})
        self.assertEqual(replaced['decision'], 'route_replaced')
        seal = run(self.db, 'counter', {'event_id': 'seal-old', 'op': 'seal',
            'authorization': AUTH, 'device': OLD, 'ends': 15})
        self.assertEqual((seal['decision'], seal['units']), ('counter_cycle_sealed', 7))
        self.assertTrue(replay(str(self.path))['verified'])

    # 6. late zero after a valid seal forces review, blocks reopen ------------ #
    def test_late_zero_executed_after_seal_reopens_period_and_blocks_next_cycle(self):
        self.authorize()
        a, b = self.run_devices()
        cycle = self.open_cycle_id(OLD)
        self.first_reading(a, 'a-r98', 11, cycle)
        self.do_work(a, 'a-r02', 12, 4, cycle)
        self.do_work(a, 'a-r05', 13, 3, cycle)
        # The previous shift's zero is queued but has not executed; the witnessed
        # chain still allows the past window to be sealed.
        self.agent.enqueue('cmd-zero-late', OLD, 'zero', 14)
        run(self.db, 'counter', {'event_id': 'q-late', 'op': 'command',
            'authorization': AUTH, 'device': OLD, 'action': 'enqueue',
            'id': 'cmd-zero-late', 'kind': 'zero', 'at': 14})
        seal = run(self.db, 'counter', {'event_id': 'seal-ok', 'op': 'seal',
            'authorization': AUTH, 'device': OLD, 'ends': 13})
        self.assertEqual(seal['decision'], 'counter_cycle_sealed')
        # The room executes the queued zero afterwards; the reply is lost.
        self.agent.deliver('cmd-zero-late', lost_reply=True)
        reported = run(self.db, 'counter', {'event_id': 'report-late', 'op': 'command',
            'authorization': AUTH, 'device': OLD, 'action': 'report',
            'id': 'cmd-zero-late', 'at': 15})
        self.assertEqual(reported['decision'], 'command_reported')
        # The previously sealed physical period goes to review and nothing new can open.
        self.assertEqual(one(self.db, "SELECT status FROM counter_cycles WHERE id=?",
                         (cycle,))['status'], 'review')
        self.assertEqual(run(self.db, 'counter', {'event_id': 'reopen', 'op': 'open_cycle',
            'authorization': AUTH, 'device': OLD, 'id': 'cyc-after',
            'at': 16})['decision'], 'counter_review_blocked')
        self.assertEqual(run(self.db, 'counter', {'event_id': 'replace-after',
            'op': 'route', 'authorization': AUTH, 'slot': SLOT,
            'choice': 'replace', 'at': 16})['decision'], 'counter_review_blocked')
        self.assertTrue(replay(str(self.path))['verified'])


if __name__ == '__main__':
    unittest.main()
