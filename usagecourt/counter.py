"""Offline accelerator meter-swap control for the settlement dispute desk.

Two layers are deliberately kept apart:

* :class:`Device`, :class:`DatacenterAgent` and :class:`Coordinator` simulate the
  machine room.  The legacy device wire interface is immutable (read remainder /
  stop intake / zero), carries neither a counter period nor a task number, and a
  successfully executed stop or zero may still lose its reply.  The agent's
  command queue is durable *inside the simulation* but never participates in a
  SQLite transaction.  The coordinator is a local isolated device entry that may
  only record facts it can itself prove.
* :func:`handle` is the event-sourced decision layer.  It runs inside the
  normal ``store.run`` transaction, decides whether a physical counter period can
  be sealed, and never reaches back into the simulated machine room.

A remainder byte alone cannot be sealed: modulo-100 wrap and a confirmed zero
produce identical bytes.  Sealing requires a continuous chain of coordinator
witnessed reads; an unconfirmed or unwitnessed zero keeps the remainder
unsealable and blocks opening the next period on that device.
"""
from .store import one, rows

MOD = 100

SCHEMA = '''
CREATE TABLE IF NOT EXISTS counter_authorizations(id TEXT PRIMARY KEY, slot TEXT NOT NULL,
   tenant TEXT NOT NULL, cluster TEXT NOT NULL, identity TEXT NOT NULL, uid TEXT NOT NULL,
   generation INTEGER NOT NULL, route TEXT, active_device TEXT, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS counter_devices(device TEXT PRIMARY KEY, slot TEXT NOT NULL,
   model TEXT NOT NULL, installed_at INTEGER NOT NULL, accepting INTEGER NOT NULL, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS counter_commands(id TEXT PRIMARY KEY, device TEXT NOT NULL,
   kind TEXT NOT NULL, queued_at INTEGER NOT NULL, state TEXT NOT NULL, receipt TEXT);
CREATE TABLE IF NOT EXISTS counter_cycles(id TEXT PRIMARY KEY, device TEXT NOT NULL, slot TEXT NOT NULL,
   uid TEXT NOT NULL, started_at INTEGER NOT NULL, ends_at INTEGER, basis TEXT,
   units INTEGER, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS counter_evidence(id TEXT PRIMARY KEY, device TEXT NOT NULL,
   cycle TEXT, kind TEXT NOT NULL, at INTEGER NOT NULL, value INTEGER, source TEXT NOT NULL, detail TEXT);
CREATE TABLE IF NOT EXISTS counter_readings(id TEXT PRIMARY KEY, device TEXT NOT NULL, slot TEXT NOT NULL,
   uid TEXT NOT NULL, at INTEGER NOT NULL, value INTEGER NOT NULL, source TEXT, state TEXT NOT NULL);
'''
TABLES = ('counter_authorizations', 'counter_devices', 'counter_commands',
          'counter_cycles', 'counter_evidence', 'counter_readings')


# --------------------------------------------------------------------------- #
# Machine-room simulation: immutable wire interface, durable agent queue, and a
# local coordinator entry.  None of this touches the metering database, so the
# agent and the ledger can never share a transaction.
# --------------------------------------------------------------------------- #

class Device:
    """A legacy accelerator counter.

    The wire surface is fixed: :meth:`read` returns the bare remainder with no
    period or task identifier, :meth:`stop` closes physical intake and
    :meth:`zero` resets the remainder.  A device may be restarted independently;
    its count and its stopped condition are durable physical state.
    """

    def __init__(self, name, value=0, blocked=False):
        self.name = name
        self.value = value % MOD
        # A spare sharing the rack position is physically installed but not yet
        # commissioned for the service UID; commissioning is a machine-room act,
        # not part of the immutable counter wire interface.
        self.stopped = blocked

    # unchanged legacy interface ------------------------------------------------
    def read(self):
        return self.value

    def stop(self):
        self.stopped = True
        return True

    def zero(self):
        self.value = 0
        return True

    # physical mechanics --------------------------------------------------------
    @property
    def accepting(self):
        return not self.stopped

    def enable(self):
        """Physical commissioning after a swap route opens the spare."""
        self.stopped = False
        return self

    def work(self, units):
        """Accept one task of ``units`` only while physically open."""
        if units <= 0 or self.stopped:
            return None
        self.value = (self.value + units) % MOD
        return self.value

    def restart(self):
        """Independent restart: count and stop state survive the reboot."""
        return self


class DatacenterAgent:
    """Durable machine-room command queue, outside the metering transaction."""

    def __init__(self):
        self.devices = {}
        self.queue = {}

    def register(self, device):
        self.devices[device.name] = device
        self.queue.setdefault(device.name, [])
        return device

    def enqueue(self, command_id, device, kind, at):
        if device not in self.devices:
            return None
        if kind not in ('stop', 'zero'):
            return None
        if any(c['kind'] == kind and c['state'] in ('queued', 'executed_ack_lost')
               for c in self.queue[device]):
            return None
        cmd = {'id': command_id, 'device': device, 'kind': kind, 'at': at,
               'state': 'queued', 'receipt': None}
        self.queue[device].append(cmd)
        return dict(cmd)

    def pending(self, device):
        return [dict(c) for c in self.queue.get(device, [])
                if c['state'] in ('queued', 'executed_ack_lost')]

    def deliver(self, command_id, lost_reply=False):
        """Execute a queued command on the real device.

        A lost reply still performs the physical side effect; the caller only
        fails to learn that it happened.
        """
        for cmd in (c for q in self.queue.values() for c in q):
            if cmd['id'] != command_id or cmd['state'] != 'queued':
                continue
            device = self.devices[cmd['device']]
            getattr(device, cmd['kind'])()
            cmd['state'] = 'executed_ack_lost' if lost_reply else 'executed'
            cmd['receipt'] = None if lost_reply else 'receipt:' + command_id
            return dict(cmd)
        return None

    def cancel(self, command_id):
        """A queued command can be withdrawn; a delivered one can never be."""
        for cmd in (c for q in self.queue.values() for c in q):
            if cmd['id'] != command_id:
                continue
            if cmd['state'] != 'queued':
                return None
            cmd['state'] = 'cancelled'
            cmd['receipt'] = 'cancel:' + command_id
            return dict(cmd)
        return None


class Coordinator:
    """Local coordination end with an isolated device entry.

    It may keep only facts it can prove through the entry itself.  The log is
    transient: an independent device restart wipes it, which is exactly how an
    unprovable period arises.
    """

    def __init__(self, name='coordinator-entry'):
        self.name = name
        self.log = {}

    def _seen(self, device):
        return self.log.setdefault(device.name, [])

    def witness_read(self, device, at):
        fact = {'kind': 'witnessed_read', 'at': at, 'value': device.read(),
                'source': self.name, 'detail': None}
        self._seen(device).append(fact)
        return dict(fact)

    def witness_stop(self, device, at):
        fact = {'kind': 'witnessed_stop', 'at': at,
                'value': 0 if device.stopped else 1, 'source': self.name,
                'detail': None}
        self._seen(device).append(fact)
        return dict(fact)

    def witness_zero(self, device, command_id, at):
        """Bind a zero to the command only when the entry itself sees 00 after
        having seen a non-zero remainder."""
        seen = self._seen(device)
        prior = next((f for f in reversed(seen) if f['kind'] == 'witnessed_read'), None)
        if device.read() != 0 or prior is None or prior['value'] == 0:
            return None
        fact = {'kind': 'witnessed_zero', 'at': at, 'value': 0,
                'source': self.name, 'detail': command_id}
        seen.append(fact)
        return dict(fact)

    def note_restart(self, device, at):
        # The reboot destroys everything the transient entry had not lodged.
        self.log[device.name] = [{'kind': 'restart_gap', 'at': at, 'value': None,
                                  'source': self.name, 'detail': None}]
        return dict(self.log[device.name][0])


# --------------------------------------------------------------------------- #
# Event-sourced decision layer
# --------------------------------------------------------------------------- #

def _auth_for_slot(db, slot):
    return one(db, 'SELECT * FROM counter_authorizations WHERE slot=? ORDER BY generation DESC', (slot,))


def _device(db, name):
    return one(db, 'SELECT * FROM counter_devices WHERE device=?', (name,))


def _open_cycle(db, device):
    return one(db, "SELECT * FROM counter_cycles WHERE device=? AND status='open' ORDER BY started_at",
               (device,))


def _zero_commands(db, device):
    return rows(db, "SELECT * FROM counter_commands WHERE device=? AND kind='zero' ORDER BY queued_at",
                (device,))


def _pending_zero(db, device):
    return [c for c in _zero_commands(db, device)
            if c['state'] in ('queued', 'executed_ack_lost')]


def _witnessed_zero_ids(db, device, ends):
    return {e['detail'] for e in rows(
        db, "SELECT detail FROM counter_evidence WHERE device=? AND kind='witnessed_zero' AND at<=?",
        (device, ends)) if e['detail']}


def continuity(db, device, cycle, ends):
    """Return (units, basis) from the coordinator chain, or None when the
    remainder cannot be distinguished from a post-zero new period."""
    chain = rows(
        db, "SELECT * FROM counter_evidence WHERE device=? AND cycle=? AND kind IN ('witnessed_read','witnessed_zero') "
        "AND at<=? ORDER BY at, id", (device, cycle, ends))
    reads = [e for e in chain if e['kind'] == 'witnessed_read']
    if not reads:
        return None
    # An executed-but-unconfirmed zero with no matching coordinator witness is
    # indistinguishable: the same 05 byte may be old-period remainder or the
    # first reading of a new period.
    witnessed = _witnessed_zero_ids(db, device, ends)
    for cmd in _pending_zero(db, device):
        if cmd['state'] == 'executed_ack_lost' and cmd['id'] not in witnessed:
            return None
    total = 0
    prev = None
    for event in chain:
        if event['kind'] == 'witnessed_read':
            value = event['value']
            if prev is not None:
                delta = value - prev
                if delta < 0:
                    delta += MOD
                total += delta
            prev = value
        else:  # witnessed zero: proven reset, remainder starts again from zero
            prev = 0
    return total, 'coordinator_chain'


def handle(db, cmd):
    op = cmd['op']

    if op == 'authorize':
        old = one(db, 'SELECT * FROM counter_authorizations WHERE id=?', (cmd['id'],))
        if old:
            return {'decision': 'authorization_exists'}
        prior = _auth_for_slot(db, cmd['slot'])
        if prior and prior['state'] != 'closed':
            return {'decision': 'slot_authorization_active'}
        for role, model, at in (('old', cmd.get('old_model', 'legacy'), cmd['at']),
                                ('backup', cmd.get('backup_model', 'spare'), cmd['at'])):
            name = cmd[role + '_device']
            if _device(db, name):
                return {'decision': 'device_already_installed'}
            db.execute('INSERT INTO counter_devices VALUES(?,?,?,?,?,?)',
                       (name, cmd['slot'], model, at, 1 if role == 'old' else 0,
                        'active' if role == 'old' else 'blocked'))
        db.execute('INSERT INTO counter_authorizations VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (cmd['id'], cmd['slot'], cmd['tenant'], cmd['cluster'],
                    cmd['identity'], cmd['uid'], cmd['generation'], None,
                    cmd['old_device'], 'open'))
        cycle = cmd['id'] + ':cycle-1'
        db.execute('INSERT INTO counter_cycles VALUES(?,?,?,?,?,?,?,?,?)',
                   (cycle, cmd['old_device'], cmd['slot'], cmd['uid'],
                    cmd['at'], None, None, None, 'open'))
        return {'decision': 'authorized', 'slot': cmd['slot'],
                'active_device': cmd['old_device'], 'cycle': cycle}

    auth = one(db, 'SELECT * FROM counter_authorizations WHERE id=?', (cmd['authorization'],)) \
        if cmd.get('authorization') else None
    if not auth and cmd.get('slot'):
        auth = _auth_for_slot(db, cmd['slot'])
    if not auth:
        return {'decision': 'unknown_authorization'}

    if op == 'rotate':
        if cmd['generation'] <= auth['generation']:
            return {'decision': 'stale_identity'}
        db.execute('UPDATE counter_authorizations SET uid=?, generation=? WHERE id=?',
                   (cmd['uid'], cmd['generation'], auth['id']))
        return {'decision': 'rotated', 'uid': cmd['uid'], 'generation': cmd['generation']}

    if op == 'route':
        if auth['state'] == 'review':
            return {'decision': 'counter_review_blocked'}
        old_device = auth['active_device']
        if cmd['choice'] == 'retain':
            # The old device keeps physically accepting; the spare must not.
            db.execute("UPDATE counter_devices SET accepting=0, state='blocked' WHERE slot=? AND device<>?",
                       (auth['slot'], old_device))
            db.execute("UPDATE counter_devices SET accepting=1, state='active' WHERE device=?",
                       (old_device,))
            db.execute("UPDATE counter_authorizations SET route='retain' WHERE id=?", (auth['id'],))
            return {'decision': 'route_retained', 'active_device': old_device}
        if cmd['choice'] == 'replace':
            stop = one(db, "SELECT * FROM counter_evidence WHERE device=? AND kind='witnessed_stop' "
                           "ORDER BY at DESC", (old_device,))
            pending_stop = rows(db, "SELECT id FROM counter_commands WHERE device=? AND kind='stop' "
                                    "AND state IN ('queued','executed_ack_lost')", (old_device,))
            # A stop whose reply was lost is still open until the isolated entry
            # independently proves the machine really closed intake.
            if pending_stop and not stop:
                return {'decision': 'counter_stop_pending', 'command': pending_stop[0]['id']}
            if _pending_zero(db, old_device):
                return {'decision': 'counter_zero_pending'}
            if not stop:
                return {'decision': 'physical_intake_not_closed'}
            spare = one(db, "SELECT * FROM counter_devices WHERE slot=? AND device<>?",
                        (auth['slot'], old_device))
            # The choice never fabricates acceptance: the spare only becomes the
            # active device, its first real task opens the physical period.
            db.execute("UPDATE counter_devices SET accepting=0, state='stopped' WHERE device=?",
                       (old_device,))
            db.execute("UPDATE counter_devices SET accepting=1, state='active' WHERE device=?",
                       (spare['device'],))
            cycle = auth['id'] + ':' + spare['device'] + '-cycle-1'
            db.execute('INSERT INTO counter_cycles VALUES(?,?,?,?,?,?,?,?,?)',
                       (cycle, spare['device'], auth['slot'], auth['uid'],
                        cmd['at'], None, None, None, 'open'))
            db.execute("UPDATE counter_authorizations SET route='replace', active_device=? WHERE id=?",
                       (spare['device'], auth['id']))
            return {'decision': 'route_replaced', 'active_device': spare['device'], 'cycle': cycle}
        return {'decision': 'unknown_route_choice'}

    if op == 'command':
        device = _device(db, cmd['device'])
        if not device or device['slot'] != auth['slot']:
            return {'decision': 'unknown_device'}
        if cmd['action'] == 'enqueue':
            if one(db, 'SELECT 1 FROM counter_commands WHERE id=?', (cmd['id'],)):
                return {'decision': 'command_exists'}
            clash = one(db, "SELECT id FROM counter_commands WHERE device=? AND kind=? "
                            "AND state IN ('queued','executed_ack_lost')",
                        (cmd['device'], cmd['kind']))
            if clash:
                return {'decision': 'command_unconfirmed', 'command': clash['id']}
            db.execute('INSERT INTO counter_commands VALUES(?,?,?,?,?,?)',
                       (cmd['id'], cmd['device'], cmd['kind'], cmd['at'], 'queued', None))
            return {'decision': 'command_queued', 'id': cmd['id']}
        row = one(db, 'SELECT * FROM counter_commands WHERE id=?', (cmd['id'],))
        if not row:
            return {'decision': 'unknown_command'}
        if cmd['action'] == 'cancel':
            # Relabelling a delivered operation as a cancellation is forbidden.
            if row['state'] != 'queued':
                return {'decision': 'cancel_rejected_command_delivered', 'state': row['state']}
            db.execute("UPDATE counter_commands SET state='cancelled', receipt=? WHERE id=?",
                       ('cancel:' + cmd['id'], cmd['id']))
            return {'decision': 'command_cancelled', 'id': cmd['id']}
        if cmd['action'] == 'report':
            # A late machine-room report.  The physical effect happened even if
            # the reply was lost; the ledger must not pretend otherwise.
            if row['state'] not in ('queued', 'executed_ack_lost'):
                return {'decision': 'command_state_conflict', 'state': row['state']}
            db.execute("UPDATE counter_commands SET state='executed_ack_lost', receipt=NULL WHERE id=?",
                       (cmd['id'],))
            if row['kind'] == 'zero':
                db.execute("UPDATE counter_authorizations SET state='review' WHERE id=?", (auth['id'],))
                db.execute("UPDATE counter_cycles SET status='review' WHERE device=? AND status='sealed'",
                           (row['device'],))
            return {'decision': 'command_reported', 'id': cmd['id'], 'state': 'executed_ack_lost'}
        return {'decision': 'unknown_command_action'}

    if op == 'intake':
        device = _device(db, cmd['device'])
        if not device or device['slot'] != auth['slot']:
            return {'decision': 'unknown_device'}
        if cmd['kind'] not in ('witnessed_read', 'witnessed_stop', 'witnessed_zero', 'restart_gap'):
            return {'decision': 'unknown_evidence_kind'}
        if one(db, 'SELECT 1 FROM counter_evidence WHERE id=?', (cmd['id'],)):
            return {'decision': 'evidence_exists'}
        if cmd['kind'] == 'witnessed_zero' and not cmd.get('detail'):
            return {'decision': 'zero_witness_requires_command'}
        db.execute('INSERT INTO counter_evidence VALUES(?,?,?,?,?,?,?,?)',
                   (cmd['id'], cmd['device'], cmd.get('cycle'), cmd['kind'],
                    cmd['at'], cmd.get('value'), cmd['source'], cmd.get('detail')))
        return {'decision': 'evidence_recorded', 'id': cmd['id']}

    if op == 'reading':
        # Platform intake guard: a packet that still reaches the room from an old
        # device must never be booked as a reading of the current service UID.
        device = _device(db, cmd['device'])
        if not device or device['slot'] != auth['slot']:
            return {'decision': 'unknown_device'}
        current = auth['active_device'] == cmd['device'] and device['accepting'] == 1 \
            and auth['uid'] == cmd['uid'] and auth['state'] != 'review'
        state = 'recorded' if current else 'stale'
        reading_id = cmd.get('id', cmd['event_id'])
        db.execute('INSERT INTO counter_readings VALUES(?,?,?,?,?,?,?,?)',
                   (reading_id, cmd['device'], auth['slot'], cmd['uid'], cmd['at'],
                    cmd['value'], cmd.get('source'), state))
        return {'decision': 'reading_recorded' if current else 'reading_rejected_stale'}

    if op == 'open_cycle':
        if auth['state'] == 'review':
            return {'decision': 'counter_review_blocked'}
        device = _device(db, cmd['device'])
        if not device or device['slot'] != auth['slot']:
            return {'decision': 'unknown_device'}
        if _pending_zero(db, cmd['device']):
            return {'decision': 'counter_zero_pending'}
        if _open_cycle(db, cmd['device']):
            return {'decision': 'counter_cycle_already_open'}
        cycle = cmd['id']
        db.execute('INSERT INTO counter_cycles VALUES(?,?,?,?,?,?,?,?,?)',
                   (cycle, cmd['device'], auth['slot'], auth['uid'], cmd['at'],
                    None, None, None, 'open'))
        return {'decision': 'counter_cycle_open', 'id': cycle}

    if op == 'seal':
        device = _device(db, cmd['device'])
        if not device or device['slot'] != auth['slot']:
            return {'decision': 'unknown_device'}
        cycle = _open_cycle(db, cmd['device'])
        if not cycle:
            return {'decision': 'no_open_cycle'}
        chain = continuity(db, cmd['device'], cycle['id'], cmd['ends'])
        pending = _pending_zero(db, cmd['device'])
        unwitnessed = [c['id'] for c in pending if c['state'] == 'executed_ack_lost'
                       and c['id'] not in _witnessed_zero_ids(db, cmd['device'], cmd['ends'])]
        if unwitnessed:
            # An executed but unproven zero makes the remainder unknowable; the
            # slot stays in review until the physical period is reconciled.
            db.execute("UPDATE counter_authorizations SET state='review' WHERE id=?", (auth['id'],))
            return {'decision': 'counter_remainder_ambiguous', 'command': unwitnessed[0]}
        if chain is None:
            # No provable chain yet; the period simply cannot be sealed, but the
            # operator may still gather evidence rather than being forced open.
            return {'decision': 'counter_remainder_ambiguous'}
        units, basis = chain
        db.execute("UPDATE counter_cycles SET ends_at=?, basis=?, units=?, status='sealed' WHERE id=?",
                   (cmd['ends'], basis, units, cycle['id']))
        # Sealed physical evidence is offered to inspection under the UID that
        # owned the period.  It creates no invoice line and no settlement.
        sample = 'counter-sample:' + cycle['id']
        if not one(db, 'SELECT 1 FROM samples WHERE id=?', (sample,)):
            db.execute('INSERT INTO samples VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (sample, auth['identity'], cycle['uid'], auth['tenant'], 'cpu',
                        auth['cluster'], cycle['started_at'], cmd['ends'], units,
                        'coordinator-entry', 'accepted'))
        next_cycle = 'blocked_pending_zero' if pending else 'open'
        if not pending:
            seq = 1 + one(db, 'SELECT COUNT(*) n FROM counter_cycles WHERE device=?',
                          (cmd['device'],))['n']
            successor = auth['id'] + ':' + cmd['device'] + '-cycle-' + str(seq)
            db.execute('INSERT INTO counter_cycles VALUES(?,?,?,?,?,?,?,?,?)',
                       (successor, cmd['device'], auth['slot'], auth['uid'], cmd['ends'],
                        None, None, None, 'open'))
        return {'decision': 'counter_cycle_sealed', 'cycle': cycle['id'],
                'units': units, 'next_cycle': next_cycle}

    if op == 'status':
        return {'decision': 'counter_status',
                'authorization': auth,
                'devices': rows(db, 'SELECT * FROM counter_devices WHERE slot=? ORDER BY device',
                                (auth['slot'],)),
                'cycles': rows(db, 'SELECT * FROM counter_cycles WHERE slot=? ORDER BY id',
                               (auth['slot'],)),
                'commands': rows(db, 'SELECT * FROM counter_commands ORDER BY id'),
                'readings': rows(db, 'SELECT * FROM counter_readings WHERE slot=? ORDER BY id',
                                 (auth['slot'],))}

    return {'decision': 'unknown_operation'}
