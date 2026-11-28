"""Receive a two-part frame without trusting the columns of an old dual probe.

The legacy bench has two probes and a reversible connector.  A reading pair
``[left, right]`` therefore describes one of two real worlds only: the columns
may be wired straight or crossed.  The columns never measure the same
workpiece, so the admissible worlds are the two swaps of the same multiset,
never the independent cross product.  For ``[2, 10]`` the worlds are
``{A: 2, B: 10}`` and ``{A: 10, B: 2}``; ``(10, 10)`` is a fabrication.

Because the frame total is invariant under the wiring uncertainty, a sealed
bench admits a whole frame by its total.  A bench with only 11 mg of capacity
must reject a ``[2, 10]`` frame even though the readings are unchanged.

Two authorised contracts start from that same uncertain pair:

``batch``
    The material department authorises a whole-frame receipt.  Eligibility is
    the invariant total; both originals move into the sealed area and one
    frame receipt is written.  No item becomes per-item claimable stock.

``sort``
    The frame never becomes received stock.  Only after real in-place evidence
    at the measurement station or local handoff slot resolves which serial sits
    in which column is the qualified item released to the clean bench.  The
    other item still requires washing.  Every still-compatible wiring must
    agree that a released item is under the single-item limit before that item
    can be claimed.

A frame receipt covers only the originals that were still assembled at
measurement time and that actually sit at the sealed bench.  A receipt command
left in the legacy queue is not a new valid receipt once the frame has been
opened or has left stock.  The legacy readback never carries a workpiece serial:
on restart, ``position 4 received`` cannot distinguish this frame from another
frame that reused the same position.  The local handoff token is the only
evidence that can re-attribute the position; while it does not match, the
origin recorded with the readback remains the actual inventory holder.
"""
import json

from .store import one, rows

# The measured single-item standard used by the wash bench: a named item may
# leave the sealed frame only at or below this residue.
WASH_LIMIT_MG = 8

OPERATIONS = frozenset({
    'frame_measure', 'batch_authorize', 'queue_enqueue', 'queue_deliver',
    'legacy_readback', 'direction_evidence', 'sort_release', 'claim',
    'open_frame', 'frame_departs', 'sealed_custody',
})

SCHEMA = """
CREATE TABLE IF NOT EXISTS sealed_frames(frame TEXT PRIMARY KEY, serials TEXT NOT NULL,
    left_mg INTEGER NOT NULL, right_mg INTEGER NOT NULL, token TEXT NOT NULL,
    origin TEXT NOT NULL, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS frame_receipts(receipt TEXT PRIMARY KEY, frame TEXT NOT NULL,
    serials TEXT NOT NULL, total_mg INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sealed_units(frame TEXT NOT NULL, serial TEXT NOT NULL,
    location TEXT NOT NULL, claimable INTEGER NOT NULL, receipt TEXT,
    PRIMARY KEY(frame, serial));
CREATE TABLE IF NOT EXISTS sealed_direction(frame TEXT NOT NULL, column TEXT NOT NULL,
    serial TEXT NOT NULL, token TEXT NOT NULL, PRIMARY KEY(frame, column));
CREATE TABLE IF NOT EXISTS legacy_queue(queue TEXT PRIMARY KEY, frame TEXT NOT NULL,
    receipt TEXT NOT NULL, token TEXT NOT NULL, position INTEGER NOT NULL,
    status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS legacy_readbacks(seq INTEGER PRIMARY KEY AUTOINCREMENT,
    queue TEXT, position INTEGER NOT NULL, raw TEXT NOT NULL, token TEXT,
    attributed INTEGER NOT NULL, holder TEXT NOT NULL);
"""
TABLES = ('sealed_frames', 'frame_receipts', 'sealed_units', 'sealed_direction',
          'legacy_queue', 'legacy_readbacks')


def worlds(serials, left_mg, right_mg):
    """The two real worlds left by a reversible connector, never a product."""
    straight = {serials[0]: left_mg, serials[1]: right_mg}
    crossed = {serials[0]: right_mg, serials[1]: left_mg}
    return [straight] if straight == crossed else [straight, crossed]


def _frame(db, name):
    return one(db, 'SELECT * FROM sealed_frames WHERE frame=?', (name,))


def _decoded(frame):
    return dict(frame, serials=json.loads(frame['serials']))


def _reading(frame, column):
    return frame['left_mg'] if column == 'left' else frame['right_mg']


def _receipt(db, name):
    return one(db, 'SELECT * FROM frame_receipts WHERE frame=?', (name,))


def _unit(db, name, serial):
    return one(db, 'SELECT * FROM sealed_units WHERE frame=? AND serial=?',
               (name, serial))


def _set_state(db, name, state):
    db.execute('UPDATE sealed_frames SET state=? WHERE frame=?', (state, name))


def _compatible_worlds(db, frame):
    """Worlds still allowed by the in-place direction evidence."""
    evidence = rows(db, 'SELECT column,serial FROM sealed_direction WHERE frame=?',
                    (frame['frame'],))
    return [world for world in worlds(json.loads(frame['serials']),
                                      frame['left_mg'], frame['right_mg'])
            if all(world[item['serial']] == _reading(frame, item['column'])
                   for item in evidence)]


def _measure(db, cmd):
    name, serials = cmd['frame'], cmd.get('serials')
    left, right = cmd['left_mg'], cmd['right_mg']
    token = cmd.get('token')
    valid = (isinstance(name, str) and name.strip()
             and isinstance(serials, list) and len(serials) == 2
             and all(isinstance(serial, str) and serial.strip() for serial in serials)
             and serials[0] != serials[1]
             and type(left) is int and type(right) is int and left >= 0 and right >= 0
             and isinstance(token, str) and token.strip())
    if not valid:
        return {'decision': 'invalid_frame_measurement'}
    existing = _frame(db, name)
    if existing:
        if _receipt(db, name):
            changed = (json.loads(existing['serials']) != serials
                       or (existing['left_mg'], existing['right_mg']) != (left, right))
            return {'decision': 'frame_identity_conflict' if changed else 'frame_measured',
                    'receipt': _receipt(db, name)['receipt'],
                    'total_mg': existing['left_mg'] + existing['right_mg']}
        if json.loads(existing['serials']) != serials:
            return {'decision': 'frame_identity_conflict'}
        if existing['state'] != 'assembled':
            return {'decision': 'frame_not_at_measurement'}
        same_reading = (existing['left_mg'], existing['right_mg']) == (left, right)
        if same_reading:
            # Same probe pair; a new handoff slot may still replace the token,
            # but the resolved wiring evidence remains valid.
            db.execute('UPDATE sealed_frames SET token=? WHERE frame=?', (token, name))
            return {'decision': 'frame_measured',
                    'worlds': worlds(serials, left, right), 'total_mg': left + right,
                    'token_updated': existing['token'] != token}
        # A recalibrated reading is authoritative, but it invalidates the
        # in-place direction evidence: the resolved wiring no longer matches
        # the probe pair, and a later claim must reconsider every wiring.
        db.execute('UPDATE sealed_frames SET left_mg=?,right_mg=?,token=? WHERE frame=?',
                   (left, right, token, name))
        db.execute('DELETE FROM sealed_direction WHERE frame=?', (name,))
        return {'decision': 'frame_remeasured',
                'worlds': worlds(serials, left, right), 'total_mg': left + right,
                'direction_cleared': True}
    db.execute('INSERT INTO sealed_frames VALUES(?,?,?,?,?,?,?)',
               (name, json.dumps(serials), left, right, token,
                cmd.get('origin', 'origin'), 'assembled'))
    return {'decision': 'frame_measured',
            'worlds': worlds(serials, left, right), 'total_mg': left + right}


def _batch_authorize(db, cmd):
    name = cmd['frame']
    frame = _frame(db, name)
    if not frame:
        return {'decision': 'frame_unmeasured'}
    if cmd.get('authority') != 'materials':
        return {'decision': 'unauthorised_batch'}
    existing = _receipt(db, name)
    if existing:
        return {'decision': 'frame_already_received', 'receipt': existing['receipt']}
    if frame['state'] != 'assembled':
        return {'decision': 'frame_not_assembled'}
    capacity = cmd.get('capacity_mg')
    if type(capacity) is not int or capacity < 0:
        return {'decision': 'invalid_batch_capacity'}
    total = frame['left_mg'] + frame['right_mg']
    if total > capacity:
        return {'decision': 'batch_capacity_exceeded', 'total_mg': total}
    receipt = cmd['receipt']
    if one(db, 'SELECT 1 FROM frame_receipts WHERE receipt=?', (receipt,)):
        return {'decision': 'receipt_identity_conflict'}
    serials = json.loads(frame['serials'])
    db.execute('INSERT INTO frame_receipts VALUES(?,?,?,?)',
               (receipt, name, json.dumps(serials), total))
    for serial in serials:
        db.execute('INSERT INTO sealed_units VALUES(?,?,?,?,?)',
                   (name, serial, 'sealed', 0, receipt))
    _set_state(db, name, 'sealed')
    return {'decision': 'batch_received', 'receipt': receipt, 'serials': serials,
            'total_mg': total, 'claimable': []}


def _queue_enqueue(db, cmd):
    name = cmd['frame']
    frame = _frame(db, name)
    if not frame:
        return {'decision': 'frame_unmeasured'}
    if _receipt(db, name):
        return {'decision': 'frame_already_received'}
    if frame['state'] in ('opened', 'departed'):
        return {'decision': 'queue_receipt_stale'}
    if one(db, 'SELECT 1 FROM legacy_queue WHERE queue=?', (cmd['queue'],)):
        return {'decision': 'queue_item_exists'}
    db.execute('INSERT INTO legacy_queue VALUES(?,?,?,?,?,?)',
               (cmd['queue'], name, cmd['receipt'], cmd['token'],
                cmd['position'], 'waiting'))
    return {'decision': 'queue_receipt_queued'}


def _queue_deliver(db, cmd):
    item = one(db, 'SELECT * FROM legacy_queue WHERE queue=?', (cmd['queue'],))
    if not item:
        return {'decision': 'unknown_queue_item'}
    frame = _frame(db, item['frame'])
    if not frame or frame['state'] in ('opened', 'departed'):
        db.execute("UPDATE legacy_queue SET status='stale' WHERE queue=?",
                   (item['queue'],))
        return {'decision': 'queue_receipt_stale'}
    if item['status'] == 'delivered':
        return {'decision': 'queue_receipt_reused'}
    db.execute("UPDATE legacy_queue SET status='delivered' WHERE queue=?",
               (item['queue'],))
    return {'decision': 'queue_receipt_delivered'}


def _legacy_readback(db, cmd):
    """A bare position readback is attributed only by its handoff token."""
    position, token = cmd['position'], cmd.get('token')
    if cmd.get('queue'):
        candidates = rows(db, 'SELECT * FROM legacy_queue WHERE queue=? AND position=?',
                          (cmd['queue'], position))
    else:
        candidates = rows(db, 'SELECT * FROM legacy_queue WHERE position=?',
                          (position,))
    matches = [item for item in candidates if token and item['token'] == token]
    attributed = len(matches) == 1
    origin = cmd.get('origin', 'origin')
    holder = matches[0]['frame'] if attributed else origin
    db.execute('INSERT INTO legacy_readbacks(queue,position,raw,token,attributed,holder) '
               'VALUES(?,?,?,?,?,?)',
               (cmd.get('queue'), position, cmd['raw'], token,
                int(attributed), holder))
    # The legacy receipt has no workpiece serial and can never release stock.
    return {'decision': 'readback_attributed' if attributed else 'readback_unattributed',
            'raw': cmd['raw'], 'holder': holder, 'claimable': []}


def _direction_evidence(db, cmd):
    name, column = cmd['frame'], cmd.get('column')
    frame = _frame(db, name)
    if not frame:
        return {'decision': 'frame_unmeasured'}
    if frame['state'] != 'assembled':
        return {'decision': 'frame_not_at_measurement'}
    if column not in ('left', 'right'):
        return {'decision': 'invalid_column'}
    if cmd.get('token') != frame['token']:
        return {'decision': 'handoff_token_mismatch'}
    serials = json.loads(frame['serials'])
    serial = cmd.get('serial')
    if serial not in serials:
        return {'decision': 'unknown_frame_item'}
    recorded = rows(db, 'SELECT * FROM sealed_direction WHERE frame=?', (name,))
    for item in recorded:
        if item['column'] == column and item['serial'] != serial:
            return {'decision': 'direction_conflict'}
        if item['column'] != column and item['serial'] == serial:
            return {'decision': 'duplicate_serial_evidence'}
    db.execute('INSERT INTO sealed_direction VALUES(?,?,?,?) '
               'ON CONFLICT(frame,column) DO UPDATE SET serial=excluded.serial, '
               'token=excluded.token',
               (name, column, serial, cmd['token']))
    possible = _compatible_worlds(db, _frame(db, name))
    return {'decision': 'direction_evidence_recorded', 'frame': name,
            'column': column, 'serial': serial, 'possible_worlds': possible}


def _sort_release(db, cmd):
    name = cmd['frame']
    frame = _frame(db, name)
    if not frame:
        return {'decision': 'frame_unmeasured'}
    if _receipt(db, name):
        return {'decision': 'frame_already_received'}
    if frame['state'] != 'assembled':
        return {'decision': 'frame_not_assembled'}
    if cmd.get('token') != frame['token']:
        return {'decision': 'handoff_token_mismatch'}
    possible = _compatible_worlds(db, frame)
    if len(possible) != 1:
        return {'decision': 'direction_pending', 'possible_worlds': possible,
                'claimable': []}
    world = possible[0]
    serials = json.loads(frame['serials'])
    qualified = [serial for serial in serials if world[serial] <= WASH_LIMIT_MG]
    if not qualified:
        return {'decision': 'no_item_qualified', 'claimable': []}
    for serial in serials:
        clean = serial in qualified
        db.execute('INSERT INTO sealed_units VALUES(?,?,?,?,?)',
                   (name, serial, 'clean' if clean else 'bench',
                    int(clean), None))
    _set_state(db, name, 'opened')
    return {'decision': 'sort_released', 'frame': name,
            'released': qualified,
            'needs_wash': [serial for serial in serials if serial not in qualified],
            'claimable': qualified, 'received_stock': []}


def _claim(db, cmd):
    name, serial = cmd['frame'], cmd['serial']
    frame = _frame(db, name)
    if not frame:
        return {'decision': 'frame_unmeasured'}
    unit = _unit(db, name, serial)
    if not unit:
        return {'decision': 'unit_unknown'}
    if unit['location'] != 'clean' or not unit['claimable']:
        return {'decision': 'not_claimable', 'location': unit['location']}
    # Even an already-cleaned item stays unclaimable if any still-compatible
    # wiring would put it over the single-item limit.
    possible = _compatible_worlds(db, frame)
    if any(world[serial] > WASH_LIMIT_MG for world in possible):
        return {'decision': 'not_claimable', 'location': unit['location'],
                'reason': 'compatible_wiring_over_limit'}
    return {'decision': 'item_claimable', 'frame': name, 'serial': serial,
            'location': 'clean'}


def _open(db, cmd):
    name = cmd['frame']
    frame = _frame(db, name)
    if not frame:
        return {'decision': 'frame_unmeasured'}
    if frame['state'] in ('opened', 'departed'):
        return {'decision': 'frame_' + frame['state']}
    _set_state(db, name, 'opened')
    return {'decision': 'frame_opened', 'receipt_voided': bool(_receipt(db, name))}


def _depart(db, cmd):
    name = cmd['frame']
    if not _frame(db, name):
        return {'decision': 'frame_unmeasured'}
    _set_state(db, name, 'departed')
    return {'decision': 'frame_departed'}


def _custody(db, cmd):
    name = cmd['frame']
    frame = _frame(db, name)
    if not frame:
        return {'decision': 'frame_unmeasured'}
    receipt = _receipt(db, name)
    units = rows(db, 'SELECT serial,location,claimable,receipt FROM sealed_units '
                     'WHERE frame=? ORDER BY serial', (name,))
    return {'decision': 'sealed_custody', 'frame': name, 'state': frame['state'],
            'total_mg': frame['left_mg'] + frame['right_mg'],
            'possible_worlds': _compatible_worlds(db, frame),
            'receipt': receipt['receipt'] if receipt else None,
            'units': units,
            'claimable': [unit['serial'] for unit in units if unit['claimable']],
            'holder': 'repository' if receipt else frame['origin']}


_HANDLERS = {
    'frame_measure': _measure,
    'batch_authorize': _batch_authorize,
    'queue_enqueue': _queue_enqueue,
    'queue_deliver': _queue_deliver,
    'legacy_readback': _legacy_readback,
    'direction_evidence': _direction_evidence,
    'sort_release': _sort_release,
    'claim': _claim,
    'open_frame': _open,
    'frame_departs': _depart,
    'sealed_custody': _custody,
}


def handle(db, command):
    handler = _HANDLERS.get(command.get('op'))
    return handler(db, command) if handler else {'decision': 'unknown_operation'}
