"""Software entitlements and the two-cluster seat-transfer protocol.

Each cluster runs its own agent with its own database.  Nothing here writes one
cluster's rows from another cluster's transaction.  A move is a sequence of
signed messages that each agent records locally:

* ``approve``      one administrator approval for one seat, source -> target.
* ``transfer``     deliver a *copy* of the entitlement.  A copy is permission to
                   attempt a handoff; it grants no seat by itself.  The source
                   records it ``pending``, the destination ``delivered``.
* ``stop_evidence`` both sites record the same proof that the source session
                   reached a stoppable boundary with a named restore point.
                   Recording the proof does not stop anything.
* ``handoff``      the origin elects to use that proof: it stops its own session
                   at the boundary, marks its copy ``armed``, and signs an
                   attestation naming the stopped session, UID, boundary and
                   restore point.  From this moment the origin may not reopen,
                   even if the destination has not confirmed a new session.
                   The destination receives the same message with that
                   attestation; it keeps its copy ``delivered`` and does not
                   infer a stop from the copy alone.
* ``complete``     the destination consumes its ``delivered`` copy with the
                   attestation and opens exactly one session, bound to its own
                   metered instance UID and actual cluster.  It returns a
                   receipt; the origin accepts the receipt and confirms the
                   stop without opening or reopening anything.

The same approval and the same stoppable-point proof also support the opposite
lawful choice: ``leave`` keeps the live session running at the origin and marks
the target's copy ``declined``.  The target then still holds only a copy and
may not check out, complete, or otherwise claim the seat.

Authority, per licence and tenant, is ``source`` (the origin may issue),
``target`` (the destination alone may issue) or ``both`` (the pre-protocol
single-database behaviour).  Only a destination that actually completed moves
it to ``target``.  A delivered copy, an acknowledgement without the origin's
handoff, a rebuilt identity that reuses an instance name, or a stale restore
record can never move it or retire a session.

Every message is applied through :func:`usagecourt.store.run`, so each agent's
event log replays into an empty database and reproduces every decision and
every local table exactly.  Test scenarios use two independent databases; no
single-database transaction stands in for a two-site commit.

Trust model: the attestation and receipt are messages, not cryptographic
signatures.  The protocol defends against stale, reordered, duplicated and
withdrawn messages, and against an agent inferring a right from its own copy.
It does not defend against an agent that fabricates the other site's message
from scratch — the synthetic supplier credentials are shared test material,
not a secret.  A production system would replace the field comparison on the
attestation with a signature check.
"""
from .store import one, rows

SCHEMA = '''
CREATE TABLE IF NOT EXISTS licenses(id TEXT PRIMARY KEY, tenant TEXT NOT NULL, product TEXT NOT NULL,
   cluster TEXT NOT NULL, seats INTEGER NOT NULL, origin TEXT);
CREATE TABLE IF NOT EXISTS license_sessions(id TEXT PRIMARY KEY, license TEXT NOT NULL,
   identity TEXT NOT NULL, uid TEXT NOT NULL, state TEXT NOT NULL, cluster TEXT,
   credential TEXT, stopped_at INTEGER, checkpoint TEXT, resume_uid TEXT, transferred TEXT);
CREATE TABLE IF NOT EXISTS license_agents(id TEXT PRIMARY KEY, cluster TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS license_approvals(id TEXT PRIMARY KEY, license TEXT NOT NULL,
   tenant TEXT NOT NULL, source TEXT NOT NULL, target TEXT NOT NULL, seat_id TEXT NOT NULL,
   issued INTEGER NOT NULL, credential TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS license_transfers(id TEXT PRIMARY KEY, approval TEXT NOT NULL,
   license TEXT NOT NULL, tenant TEXT NOT NULL, source TEXT NOT NULL, target TEXT NOT NULL,
   seat_id TEXT NOT NULL, credential TEXT NOT NULL, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS license_evidence(id TEXT PRIMARY KEY, license TEXT NOT NULL,
   tenant TEXT NOT NULL, agent TEXT NOT NULL, session TEXT NOT NULL, cluster TEXT NOT NULL,
   uid TEXT NOT NULL, restore_uid TEXT, boundary INTEGER NOT NULL, credential TEXT NOT NULL,
   kind TEXT, attested TEXT);
CREATE TABLE IF NOT EXISTS license_authority(license TEXT NOT NULL, tenant TEXT NOT NULL,
   mode TEXT NOT NULL, reason TEXT, approval TEXT, credential TEXT,
   PRIMARY KEY(license, tenant));
CREATE TABLE IF NOT EXISTS license_decisions(id TEXT PRIMARY KEY, license TEXT NOT NULL,
   tenant TEXT NOT NULL, agent TEXT NOT NULL, subject TEXT, state TEXT, detail TEXT);
'''
TABLES = ('licenses', 'license_sessions', 'license_agents', 'license_approvals',
          'license_transfers', 'license_evidence', 'license_authority', 'license_decisions')
# Message-derived copies; a single agent's log can rebuild its own decisions.
FOREIGN_TABLES = ('license_approvals', 'license_transfers', 'license_evidence')

OPEN = ('active', 'paused')          # states that occupy a seat
PARKED = ('pending', 'delivered')


# --- schema compatibility and small helpers -----------------------------------

def _columns(db, table):
    return {r[1] for r in db.execute('PRAGMA table_info(%s)' % table)}


def _ensure(db):
    """Upgrade a database created by the pre-protocol licence schema."""
    fields = _columns(db, 'license_sessions')
    for name, kind in (('cluster', 'TEXT'), ('credential', 'TEXT'), ('stopped_at', 'INTEGER'),
                       ('checkpoint', 'TEXT'), ('resume_uid', 'TEXT'), ('transferred', 'TEXT')):
        if name not in fields:
            db.execute('ALTER TABLE license_sessions ADD COLUMN %s %s' % (name, kind))
    if 'origin' not in _columns(db, 'licenses'):
        db.execute('ALTER TABLE licenses ADD COLUMN origin TEXT')
    if 'attested' not in _columns(db, 'license_evidence'):
        db.execute('ALTER TABLE license_evidence ADD COLUMN attested TEXT')


def _agent(db):
    """(agent id, cluster) when this database is bound to exactly one agent."""
    found = rows(db, 'SELECT id, cluster FROM license_agents ORDER BY id')
    return (found[0]['id'], found[0]['cluster']) if len(found) == 1 else (None, None)


def _cluster(db):
    return _agent(db)[1]


def _home(db, policy):
    """The cluster this database answers for when unbound: the licence origin."""
    return _cluster(db) or policy['origin'] or policy['cluster']


def _policy(db, license_id, tenant=''):
    if not license_id:
        return None
    if tenant:
        return one(db, 'SELECT * FROM licenses WHERE id=? AND tenant=?', (license_id, tenant))
    found = rows(db, 'SELECT * FROM licenses WHERE id=? ORDER BY tenant', (license_id,))
    return found[0] if len(found) == 1 else None


def _session(db, session_id):
    return one(db, 'SELECT * FROM license_sessions WHERE id=?', (session_id,)) if session_id else None


def _evidence(db, evidence_id):
    return one(db, 'SELECT * FROM license_evidence WHERE id=?', (evidence_id,)) if evidence_id else None


def _approval(db, approval_id):
    return one(db, 'SELECT * FROM license_approvals WHERE id=?', (approval_id,)) if approval_id else None


def _transfer(db, transfer_id):
    return one(db, 'SELECT * FROM license_transfers WHERE id=?', (transfer_id,)) if transfer_id else None


def _open_sessions(db, license_id, cluster=None):
    sql = 'SELECT * FROM license_sessions WHERE license=? AND state IN (?,?)'
    args = [license_id, 'active', 'paused']
    if cluster is not None:
        sql += ' AND cluster=?'
        args.append(cluster)
    return rows(db, sql + ' ORDER BY id', tuple(args))


def _reject(db, cmd, reason):
    db.execute('INSERT INTO license_decisions VALUES(?,?,?,?,?,?,?)',
               (cmd.get('event_id'), cmd.get('license') or '', cmd.get('tenant') or '',
                _agent(db)[0] or '', cmd.get('id') or cmd.get('session') or cmd.get('transfer'),
                'denied', reason))
    return {'decision': reason}


def _entitled(db, identity, uid, tenant, cluster):
    """A seat may only be opened for the instance ``meter`` recorded here."""
    owner = one(db, 'SELECT uid, tenant, cluster FROM identities WHERE id=?', (identity,))
    return bool(owner) and (owner['uid'], owner['tenant'], owner['cluster']) == \
        (uid, tenant, cluster)


def _accept(db, cmd, subject, state, detail):
    db.execute('INSERT INTO license_decisions VALUES(?,?,?,?,?,?,?)',
               (cmd.get('event_id'), cmd.get('license') or '', cmd.get('tenant') or '',
                _agent(db)[0] or '', subject, state, detail))


# --- authority ----------------------------------------------------------------

def _mode(db, license_id, tenant):
    found = one(db, 'SELECT mode FROM license_authority WHERE license=? AND tenant=?',
                (license_id, tenant))
    return found['mode'] if found else 'source'


def _set_authority(db, license_id, tenant, mode, reason, approval=None, credential=''):
    db.execute('INSERT INTO license_authority VALUES(?,?,?,?,?,?) '
               'ON CONFLICT(license,tenant) DO UPDATE SET mode=excluded.mode, reason=excluded.reason, '
               'approval=excluded.approval, credential=excluded.credential',
               (license_id, tenant, mode, reason, approval, credential))


def _authority_state(db, policy, cluster=None):
    """Who may issue now, who owns the next exclusive issue, and why not.

    A delivered *copy* is not a stop.  So while the transfer is only
    ``pending`` (origin's view) or ``delivered`` (destination's view):
      * the origin may keep issuing — its current session was never stopped;
      * the destination may not, because it holds no usable seat yet.
    Once the origin ``armed`` the handoff it has stopped its own session, and
    neither site may issue until the destination completes.  After
    ``completed``, only the destination issues (when authority is ``target``).
    ``declined`` and ``revoked`` transfers release the origin.
    """
    license_id, tenant = policy['id'], policy['tenant']
    mode = _mode(db, license_id, tenant)
    origin = policy['origin'] or policy['cluster']
    cluster = cluster or _home(db, policy)
    transfers = rows(db, 'SELECT * FROM license_transfers WHERE license=? AND tenant=? ORDER BY id',
                     (license_id, tenant))
    completed = next((t for t in transfers if t['state'] == 'completed'), None)
    armed = next((t for t in transfers if t['state'] == 'armed'), None)
    copy = next((t for t in transfers if t['state'] in ('pending', 'delivered')), None)
    if completed:
        issuer = completed['target'] if mode == 'target' else None
        return {'mode': mode, 'origin': origin, 'issuer': issuer,
                'blocked': issuer is None, 'next': completed['target'],
                'pending': None, 'armed': None}
    if armed:
        return {'mode': mode, 'origin': origin, 'issuer': None, 'blocked': True,
                'next': armed['target'], 'pending': armed['target'], 'armed': armed['target']}
    if copy:
        # Only the origin keeps issuing; the destination still has no seat.
        origin_may = cluster == origin and mode in ('both', 'source')
        return {'mode': mode, 'origin': origin,
                'issuer': origin if origin_may else None, 'blocked': not origin_may,
                'next': origin if origin_may else copy['target'],
                'pending': copy['target'], 'armed': None}
    if mode == 'both':
        return {'mode': mode, 'origin': origin, 'issuer': None, 'blocked': False,
                'next': origin, 'pending': None, 'armed': None}
    if mode == 'source':
        return {'mode': mode, 'origin': origin, 'issuer': origin, 'blocked': False,
                'next': origin, 'pending': None, 'armed': None}
    return {'mode': mode, 'origin': origin, 'issuer': None, 'blocked': True,
            'next': None, 'pending': None, 'armed': None}


def _may_issue(state, cluster):
    return not state['blocked'] and state['issuer'] in (None, cluster)


# --- dispatch -----------------------------------------------------------------

def handle(db, cmd):
    _ensure(db)
    op = cmd['op']
    if op == 'register':
        return _register(db, cmd)
    if op == 'join':
        return _join(db, cmd)
    if op == 'approve':
        return _approve(db, cmd)
    if op == 'transfer':
        return _deliver_copy(db, cmd)
    if op == 'stop_evidence':
        return _record_proof(db, cmd)
    if op == 'handoff':
        return _handoff(db, cmd)
    if op == 'complete' or op == 'confirm':
        return _complete(db, cmd)
    if op == 'leave':
        return _leave(db, cmd)
    if op == 'resume':
        return _resume(db, cmd)
    if op == 'revoke':
        return _revoke(db, cmd)
    if op == 'probe':
        return _probe(db, cmd)
    if op == 'audit':
        return _audit(db, cmd)
    if op in ('checkout', 'checkin', 'checkpoint'):
        policy = _policy(db, cmd.get('license'), cmd.get('tenant'))
        if not policy:
            return _reject(db, cmd, 'unknown_license')
        if op == 'checkout':
            return _checkout(db, cmd, policy)
        if op == 'checkin':
            return _checkin(db, cmd, policy)
        return _checkpoint(db, cmd, policy)
    return _reject(db, cmd, 'unknown_operation')


# --- protocol messages ---------------------------------------------------------

def _register(db, cmd):
    if cmd['seats'] < 1:
        return _reject(db, cmd, 'invalid_seats')
    if _policy(db, cmd['id'], cmd['tenant']):
        return _reject(db, cmd, 'duplicate_license')
    origin = cmd.get('origin') or cmd['cluster']
    db.execute('INSERT INTO licenses VALUES(?,?,?,?,?,?)',
               (cmd['id'], cmd['tenant'], cmd['product'], cmd['cluster'], cmd['seats'], origin))
    _set_authority(db, cmd['id'], cmd['tenant'], 'source', 'registered (' + origin + ')')
    return {'decision': 'license_registered', 'origin': origin}


def _join(db, cmd):
    if not cmd.get('agent') or not cmd.get('cluster'):
        return _reject(db, cmd, 'invalid_agent')
    found = rows(db, 'SELECT id, cluster FROM license_agents ORDER BY id')
    wanted = (cmd['agent'], cmd['cluster'])
    if found and (len(found) != 1 or (found[0]['id'], found[0]['cluster']) != wanted):
        return _reject(db, cmd, 'agent_already_bound')
    db.execute('INSERT INTO license_agents VALUES(?,?) ON CONFLICT(id) DO NOTHING', wanted)
    return {'decision': 'agent_joined', 'agent': cmd['agent'], 'cluster': cmd['cluster']}


def _approve(db, cmd):
    """One administrator approval: one seat, source -> target, never both."""
    cluster = _cluster(db)
    license_id, tenant = cmd.get('license'), cmd.get('tenant')
    if not cmd.get('approval') or not cmd.get('credential'):
        return _reject(db, cmd, 'approval_unsigned')
    if cmd.get('source') == cmd.get('target'):
        return _reject(db, cmd, 'invalid_target')
    old = _approval(db, cmd['approval'])
    if old:
        if (old['license'], old['source'], old['target'], old['credential']) == \
                (license_id, cmd['source'], cmd['target'], cmd['credential']):
            return {'decision': 'transfer_approved', 'approval': old['id'],
                    'source': old['source'], 'target': old['target'], 'quantity': 1,
                    'copy': True}
        return _reject(db, cmd, 'duplicate_approval')
    policy = _policy(db, license_id, tenant)
    if not policy:
        return _reject(db, cmd, 'unknown_license')
    if policy['seats'] != 1:
        # A transfer moves *the* one seat.  A multi-seat licence keeps issuing
        # locally; transferring one of several seats is not modelled.
        return _reject(db, cmd, 'transfer_requires_single_seat')
    origin = policy['origin'] or policy['cluster']
    if cluster == origin:
        if cmd['source'] != origin or cmd['target'] == origin:
            return _reject(db, cmd, 'invalid_target')
    elif cluster == cmd.get('target'):
        if cmd['source'] != origin or cmd['target'] != cluster:
            return _reject(db, cmd, 'invalid_target')
    else:
        return _reject(db, cmd, 'not_issuer')
    db.execute('INSERT INTO license_approvals VALUES(?,?,?,?,?,?,?,?)',
               (cmd['approval'], license_id, tenant, cmd['source'], cmd['target'],
                cmd.get('seat_id') or license_id, cmd.get('issued', 0), cmd['credential']))
    _accept(db, cmd, cmd['approval'], 'approved',
            'one seat %s -> %s' % (cmd['source'], cmd['target']))
    return {'decision': 'transfer_approved', 'approval': cmd['approval'],
            'source': cmd['source'], 'target': cmd['target'], 'quantity': 1}


def _deliver_copy(db, cmd):
    """Deliver an entitlement copy.  This is permission to try, not a seat."""
    cluster = _cluster(db)
    approval = _approval(db, cmd.get('approval'))
    if not approval:
        return _reject(db, cmd, 'unknown_approval')
    known = _transfer(db, cmd.get('id'))
    if known:
        if (known['approval'], known['credential']) != (approval['id'], cmd.get('credential')):
            return _reject(db, cmd, 'transfer_conflict')
        return {'decision': 'transfer_delivered', 'transfer': known['id'],
                'approval': approval['id'], 'state': known['state'], 'copy': True,
                'target': known['target'], 'quantity': 1}
    if not cmd.get('id') or not cmd.get('credential'):
        return _reject(db, cmd, 'invalid_transfer')
    if cmd['credential'] != approval['credential']:
        return _reject(db, cmd, 'credential_mismatch')
    policy = _policy(db, approval['license'], approval['tenant'])
    if not policy:
        return _reject(db, cmd, 'unknown_license')
    origin = policy['origin'] or policy['cluster']
    if one(db, 'SELECT id FROM license_transfers WHERE approval=?', (approval['id'],)):
        return _reject(db, cmd, 'transfer_in_progress')
    if cluster == origin:
        state = _authority_state(db, policy, cluster)
        if state['blocked'] or state['issuer'] not in (None, cluster):
            return _reject(db, cmd, 'authority_conflict')
        copy_state = 'pending'
    elif cluster == approval['target']:
        copy_state = 'delivered'
    else:
        return _reject(db, cmd, 'not_issuer')
    db.execute('INSERT INTO license_transfers VALUES(?,?,?,?,?,?,?,?,?)',
               (cmd['id'], approval['id'], approval['license'], approval['tenant'],
                approval['source'], approval['target'], approval['seat_id'],
                cmd['credential'], copy_state))
    _accept(db, cmd, cmd['id'], copy_state,
            'copy %s -> %s' % (approval['source'], approval['target']))
    return {'decision': 'transfer_delivered', 'transfer': cmd['id'], 'approval': approval['id'],
            'state': copy_state, 'copy': True, 'target': approval['target'], 'quantity': 1}


def _record_proof(db, cmd):
    """Both sites record one proof of a stoppable boundary.  Nothing stops.

    The proof names the source session, the instance UID it is bound to, the
    boundary, and the restore point that would be legal afterwards.  It never
    closes a session and never edits a copy; only ``handoff`` acts on it.
    """
    cluster = _cluster(db)
    policy = _policy(db, cmd.get('license'), cmd.get('tenant'))
    if not policy:
        return _reject(db, cmd, 'unknown_license')
    origin = policy['origin'] or policy['cluster']
    if cmd.get('uid') is None or not cmd.get('restore_uid') \
            or not isinstance(cmd.get('boundary'), int) or cmd['boundary'] < 0 \
            or not cmd.get('credential'):
        return _reject(db, cmd, 'evidence_incomplete')
    old = _evidence(db, cmd.get('id'))
    if old:
        if (old['license'], old['session'], old['uid'], old['boundary'], old['restore_uid']) == \
                (policy['id'], cmd.get('session'), cmd['uid'], cmd['boundary'], cmd['restore_uid']):
            return {'decision': 'stoppable_point_recorded', 'evidence': old['id'],
                    'session': old['session'], 'uid': old['uid'], 'boundary': old['boundary'],
                    'restore_uid': old['restore_uid'], 'stopped': False, 'replay': True}
        return _reject(db, cmd, 'evidence_conflict')
    local_session = _session(db, cmd.get('session'))
    if local_session and cluster == origin:
        if local_session['license'] != policy['id'] \
                or (local_session['cluster'] or origin) != origin:
            return _reject(db, cmd, 'unknown_session')
        if local_session['uid'] != cmd['uid']:
            return _reject(db, cmd, 'uid_mismatch')
        if local_session['state'] not in OPEN:
            return _reject(db, cmd, 'session_already_closed')
        agent = _agent(db)[0] or ''
    else:
        copy = one(db, "SELECT * FROM license_transfers WHERE license=? AND tenant=? "
                       "AND seat_id=? AND target=? AND state IN (?,?) ORDER BY id",
                   (policy['id'], policy['tenant'], cmd.get('session'), cluster, *PARKED))
        if not copy or copy['credential'] != cmd['credential']:
            return _reject(db, cmd, 'unknown_session')
        agent = _agent(db)[0] or ''
    db.execute('INSERT INTO license_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
               (cmd['id'], policy['id'], policy['tenant'], agent, cmd['session'], origin,
                cmd['uid'], cmd['restore_uid'], cmd['boundary'], cmd['credential'], 'stoppable',
                None))
    _accept(db, cmd, cmd['session'], 'stoppable',
            'boundary %s, restore %s' % (cmd['boundary'], cmd['restore_uid']))
    return {'decision': 'stoppable_point_recorded', 'evidence': cmd['id'],
            'session': cmd['session'], 'uid': cmd['uid'], 'boundary': cmd['boundary'],
            'restore_uid': cmd['restore_uid'], 'stopped': False}


def _handoff(db, cmd):
    """The origin acts on the proof: it stops its own session and signs off.

    Only the origin can arm a handoff.  The destination receives the same
    message as an attestation: it may arm its copy only when the message
    carries the origin's stoppage signature (``attested_by`` naming the origin
    and ``attested`` naming the *origin's* local session row).  The destination
    never infers that the origin stopped from its own copy alone, so it can
    never complete while the origin is still live.  Re-delivery is a guarded
    replay, not a new stop.
    """
    cluster = _cluster(db)
    transfer = _transfer(db, cmd.get('transfer'))
    if not transfer:
        return _reject(db, cmd, 'unknown_transfer')
    if cmd.get('credential') != transfer['credential']:
        return _reject(db, cmd, 'credential_mismatch')
    proof = _evidence(db, cmd.get('evidence'))
    if not proof:
        return _reject(db, cmd, 'unknown_evidence')
    if proof['license'] != transfer['license'] or proof['session'] != transfer['seat_id'] \
            or proof['credential'] != transfer['credential']:
        return _reject(db, cmd, 'evidence_mismatch')
    policy = _policy(db, transfer['license'], transfer['tenant'])
    if not policy:
        return _reject(db, cmd, 'unknown_license')
    origin = policy['origin'] or policy['cluster']
    if proof['kind'] == 'armed':
        if transfer['state'] in ('armed', 'completed', 'revoked'):
            return {'decision': 'handoff_armed', 'transfer': transfer['id'],
                    'session': transfer['seat_id'], 'uid': proof['uid'],
                    'boundary': proof['boundary'], 'restore_uid': proof['restore_uid'],
                    'stopped_session': transfer['seat_id'], 'source': transfer['source'],
                    'next_issuer': transfer['target'], 'pending_handoff': transfer['target'],
                    'authority': _mode(db, transfer['license'], transfer['tenant']),
                    'replay': True}
    if transfer['state'] not in ('pending', 'delivered'):
        return _reject(db, cmd, 'transfer_revoked')
    if cluster == origin:
        session = _session(db, transfer['seat_id'])
        if not session or session['license'] != transfer['license'] \
                or (session['cluster'] or origin) != origin or session['uid'] != proof['uid']:
            return _reject(db, cmd, 'unknown_session')
        if session['state'] not in OPEN:
            return _reject(db, cmd, 'source_not_stopped')
        db.execute("UPDATE license_sessions SET state='closed', stopped_at=?, checkpoint='handoff', "
                   "resume_uid=? WHERE id=?",
                   (proof['boundary'], proof['restore_uid'], session['id']))
        attestation = {'attested_by': origin, 'attested': session['id'], 'uid': proof['uid'],
                       'boundary': proof['boundary'], 'restore_uid': proof['restore_uid'],
                       'credential': transfer['credential']}
    elif cluster == transfer['target']:
        attestation = cmd.get('attestation')
        if not isinstance(attestation, dict) \
                or attestation.get('attested_by') != origin \
                or attestation.get('attested') != transfer['seat_id'] \
                or attestation.get('uid') != proof['uid'] \
                or attestation.get('boundary') != proof['boundary'] \
                or attestation.get('restore_uid') != proof['restore_uid'] \
                or attestation.get('credential') != transfer['credential']:
            return _reject(db, cmd, 'attestation_required')
        # The destination only mirrors the origin's signed stop.  It holds no
        # source session rows and opens none here.
    else:
        return _reject(db, cmd, 'not_issuer')
    # The origin records the stop as ``armed``; the destination records the
    # attestation but keeps its own copy ``delivered`` until it completes.  Its
    # copy is still withdrawable, but only before completion.
    if cluster == origin:
        db.execute("UPDATE license_transfers SET state='armed' WHERE id=?", (transfer['id'],))
    db.execute("UPDATE license_evidence SET kind='armed', attested=? WHERE id=?",
               (attestation['attested'], proof['id']))
    _accept(db, cmd, transfer['seat_id'], 'armed',
            'stopped at %s; next issuer %s' % (proof['boundary'], transfer['target']))
    return {'decision': 'handoff_armed', 'transfer': transfer['id'],
            'session': transfer['seat_id'], 'uid': proof['uid'],
            'boundary': proof['boundary'], 'restore_uid': proof['restore_uid'],
            'stopped_session': transfer['seat_id'], 'source': transfer['source'],
            'next_issuer': transfer['target'], 'pending_handoff': transfer['target'],
            'authority': _mode(db, transfer['license'], transfer['tenant']),
            'attestation': attestation}


def _complete(db, cmd):
    """Consume one parked copy with one proof and the other site's signature.

    * At the destination: the message must carry the origin's stoppage
      attestation (``attested_by == origin``, naming the stopped source session
      and boundary).  Without it the destination may not open a seat, no matter
      what its own copy says.  On success it opens exactly one session and
      returns a receipt naming itself, the new session, and the closed source.
    * At the origin: only the destination's receipt is accepted.  The origin
      records the handoff and confirms the stop, but never opens a session and
      never reopens the stopped one.  A receipt that still shows the source
      open is refused.

    Either site replays the same message idempotently; a completed transfer is
    answered with its own recorded result, never with a fresh seat.
    """
    cluster = _cluster(db)
    transfer = _transfer(db, cmd.get('transfer'))
    if not transfer:
        return _reject(db, cmd, 'unknown_transfer')
    if cmd.get('credential') != transfer['credential']:
        return _reject(db, cmd, 'credential_mismatch')
    proof = _evidence(db, cmd.get('evidence'))
    if not proof:
        return _reject(db, cmd, 'evidence_required')
    policy = _policy(db, transfer['license'], transfer['tenant'])
    if not policy:
        return _reject(db, cmd, 'unknown_license')
    origin = policy['origin'] or policy['cluster']
    if (proof['license'], proof['session'], proof['uid'], proof['boundary'],
            proof['restore_uid'], proof['credential']) != \
            (transfer['license'], transfer['seat_id'], proof['uid'], proof['boundary'],
             proof['restore_uid'], transfer['credential']):
        return _reject(db, cmd, 'evidence_mismatch')

    expected_attestation = {'attested_by': origin, 'attested': transfer['seat_id'],
                            'uid': proof['uid'], 'boundary': proof['boundary'],
                            'restore_uid': proof['restore_uid'],
                            'credential': transfer['credential']}

    if cluster == origin:
        receipt = cmd.get('receipt')
        if not isinstance(receipt, dict) or receipt.get('by') != transfer['target'] \
                or receipt.get('opened') != cmd.get('session') \
                or not receipt.get('uid') \
                or receipt.get('boundary') != proof['boundary'] \
                or receipt.get('credential') != transfer['credential']:
            return _reject(db, cmd, 'receipt_required')
        session = _session(db, transfer['seat_id'])
        if not session or session['uid'] != proof['uid'] \
                or (session['cluster'] or origin) != origin:
            return _reject(db, cmd, 'unknown_session')
        if session['state'] != 'closed' or session['stopped_at'] != proof['boundary']:
            return _reject(db, cmd, 'source_not_stopped')
        if transfer['state'] == 'completed':
            return {'decision': 'handoff_confirmed', 'session': session['id'],
                    'uid': session['uid'], 'cluster': transfer['target'],
                    'source': transfer['source'], 'evidence': proof['id'],
                    'stopped_session': session['id'], 'opened': receipt.get('opened'),
                    'boundary': proof['boundary'], 'restore_uid': session['resume_uid'],
                    'authority': 'target', 'issuer': transfer['target'], 'quantity': 1,
                    'replay': True}
        if transfer['state'] != 'armed':
            return _reject(db, cmd, 'source_not_armed')
        db.execute("UPDATE license_evidence SET kind='consumed' WHERE id=?", (proof['id'],))
        db.execute("UPDATE license_transfers SET state='completed' WHERE id=?", (transfer['id'],))
        db.execute("UPDATE license_sessions SET transferred=? WHERE id=?",
                   (transfer['id'], session['id']))
        _set_authority(db, transfer['license'], transfer['tenant'], 'target',
                       'handoff confirmed', transfer['approval'], cmd.get('credential', ''))
        _accept(db, cmd, session['id'], 'handed_off',
                'receipt %s from %s' % (receipt.get('opened'), transfer['target']))
        return {'decision': 'handoff_confirmed', 'session': session['id'], 'uid': session['uid'],
                'cluster': transfer['target'], 'source': transfer['source'],
                'evidence': proof['id'], 'stopped_session': session['id'],
                'opened': receipt.get('opened'), 'boundary': proof['boundary'],
                'restore_uid': session['resume_uid'], 'authority': 'target',
                'issuer': transfer['target'], 'quantity': 1}

    if cluster != transfer['target']:
        return _reject(db, cmd, 'not_target')
    if transfer['state'] == 'revoked':
        return _reject(db, cmd, 'transfer_revoked')
    if transfer['state'] == 'declined':
        return _reject(db, cmd, 'transfer_deferred')
    if transfer['state'] == 'completed':
        # Replay of a completed handoff needs no fresh attestation check; the
        # attestation was verified when the seat was opened.  A cleared or
        # withdrawn copy never reaches this branch because state is checked
        # above.
        existing = one(db, 'SELECT * FROM license_sessions WHERE license=? AND transferred=? '
                            'ORDER BY id', (policy['id'], transfer['id']))
        if existing and existing['id'] == cmd.get('session'):
            return {'decision': 'seat_transferred', 'session': existing['id'],
                    'uid': existing['uid'], 'cluster': existing['cluster'],
                    'source': transfer['source'], 'evidence': proof['id'],
                    'stopped_session': existing['resume_uid'],
                    'opened': existing['id'], 'boundary': existing['stopped_at'],
                    'restore_uid': existing['resume_uid'],
                    'authority': _mode(db, policy['id'], policy['tenant']),
                    'issuer': existing['cluster'], 'quantity': 1, 'replay': True}
        return _reject(db, cmd, 'transfer_already_complete')
    attestation = cmd.get('attestation')
    if not isinstance(attestation, dict) or {k: attestation.get(k) for k in expected_attestation} \
            != expected_attestation:
        return _reject(db, cmd, 'attestation_required')
    if transfer['state'] not in ('delivered', 'armed'):
        return _reject(db, cmd, 'source_not_stopped')
    if proof['kind'] != 'armed':
        # Merely knowing the boundary is stoppable is not a stop.  Only the
        # origin's handoff turns the proof into an usable attestation.
        return _reject(db, cmd, 'evidence_already_used')
    if not cmd.get('session'):
        return _reject(db, cmd, 'invalid_session')
    if _session(db, cmd['session']):
        return _reject(db, cmd, 'duplicate_session')
    if _open_sessions(db, policy['id'], cluster):
        return _reject(db, cmd, 'no_seat')
    # The new session uses the destination's metered instance: its own UID and
    # its actual cluster, as recorded by ``meter``.  The proof above only says
    # the *old* source session, tied to the old UID, has stopped.
    if not _entitled(db, cmd.get('identity'), cmd.get('uid'), policy['tenant'], cluster):
        return _reject(db, cmd, 'not_entitled')
    new_session = cmd['session']
    db.execute("UPDATE license_evidence SET kind='consumed', attested=? WHERE id=?",
               (transfer['seat_id'], proof['id']))
    db.execute('INSERT INTO license_sessions VALUES(?,?,?,?,?,?,?,?,?,?,?)',
               (new_session, policy['id'], cmd['identity'], cmd['uid'],
                'active', cluster, cmd.get('credential'), None, proof['restore_uid'],
                proof['id'], transfer['id']))
    db.execute("UPDATE license_transfers SET state='completed' WHERE id=?", (transfer['id'],))
    _set_authority(db, transfer['license'], transfer['tenant'], 'target',
                   'seat moved to ' + cluster, transfer['approval'], cmd.get('credential', ''))
    _accept(db, cmd, new_session, 'open',
            'seat received at %s from %s' % (cluster, transfer['source']))
    return {'decision': 'seat_transferred', 'session': new_session, 'uid': cmd['uid'],
            'cluster': cluster, 'source': transfer['source'], 'evidence': proof['id'],
            'stopped_session': proof['session'], 'stopped_uid': proof['uid'],
            'opened': new_session, 'boundary': proof['boundary'],
            'restore_uid': proof['restore_uid'], 'authority': 'target',
            'issuer': cluster, 'quantity': 1,
            'receipt': {'by': cluster, 'opened': new_session, 'uid': cmd['uid'],
                        'boundary': proof['boundary'], 'restore_uid': proof['restore_uid'],
                        'credential': transfer['credential']}}


def _leave(db, cmd):
    """Same approval, other lawful outcome: stay at the origin, keep working.

    The live source session is untouched.  The target's copy is marked
    incomplete, so the target can neither complete nor check out that seat.
    """
    cluster = _cluster(db)
    approval = _approval(db, cmd.get('approval'))
    if not approval:
        return _reject(db, cmd, 'unknown_approval')
    policy = _policy(db, approval['license'], approval['tenant'])
    if not policy:
        return _reject(db, cmd, 'unknown_license')
    origin = policy['origin'] or policy['cluster']
    copy = one(db, 'SELECT * FROM license_transfers WHERE approval=? ORDER BY id', (approval['id'],))
    if cluster == origin:
        session = _session(db, cmd.get('session') or approval['seat_id'])
        if not session or session['license'] != policy['id'] \
                or (session['cluster'] or origin) != origin:
            return _reject(db, cmd, 'unknown_session')
        if session['state'] not in OPEN:
            return _reject(db, cmd, 'session_not_open')
    elif copy and copy['target'] == cluster:
        pass  # the destination may also record the deferral it was told about
    else:
        return _reject(db, cmd, 'not_source')
    if copy and copy['state'] in ('armed', 'completed'):
        return _reject(db, cmd, 'transfer_in_progress')
    if copy:
        db.execute("UPDATE license_transfers SET state='declined' WHERE id=?", (copy['id'],))
    else:
        db.execute('INSERT INTO license_transfers VALUES(?,?,?,?,?,?,?,?,?)',
                   ('declined:' + approval['id'], approval['id'], approval['license'],
                    approval['tenant'], approval['source'], approval['target'],
                    approval['seat_id'], approval['credential'], 'declined'))
    _set_authority(db, approval['license'], approval['tenant'], 'source',
                   'elected stay; transfer incomplete', approval['id'], cmd.get('credential', ''))
    _accept(db, cmd, approval['seat_id'], 'stayed',
            'transfer %s left incomplete; origin keeps its seat' % approval['id'])
    return {'decision': 'transfer_deferred', 'approval': approval['id'],
            'transfer': 'declined', 'cluster': cluster, 'seat_cluster': origin,
            'session': approval['seat_id'], 'authority': 'source', 'issuer': origin,
            'may_issue': cluster == origin, 'next_issuer': origin}


def _resume(db, cmd):
    """Continue at the origin only from the exact restore point a stop allowed.

    Two things may stop a source session: a licence-driven ``checkpoint``, or an
    ``handoff`` that parks the seat.  A checkpoint leaves a ``checkpoint``
    marker on the closed row and no transfer; the origin may continue from the
    restore UID it named.  A handoff leaves the session closed with a proof
    that the destination may still consume; the origin may not continue until
    that handoff is withdrawn (or the transfer was declined).  A proof of a
    merely-stoppable point is never permission to continue.
    """
    cluster = _cluster(db)
    policy = _policy(db, cmd.get('license'), cmd.get('tenant'))
    if not policy:
        return _reject(db, cmd, 'unknown_license')
    origin = policy['origin'] or policy['cluster']
    if cluster != origin:
        return _reject(db, cmd, 'not_source')
    source = _session(db, cmd.get('session') or cmd.get('stopped_session'))
    if not source or source['license'] != policy['id'] or source['state'] != 'closed' \
            or source['stopped_at'] is None or (source['cluster'] or origin) != origin:
        return _reject(db, cmd, 'unknown_session')
    if not cmd.get('restore_uid') or cmd['restore_uid'] != source['resume_uid']:
        return _reject(db, cmd, 'restore_point_required')
    if cmd.get('uid') is not None and cmd['uid'] != source['uid']:
        return _reject(db, cmd, 'uid_mismatch')
    transfers = rows(db, 'SELECT * FROM license_transfers WHERE license=? AND tenant=? ORDER BY id',
                     (policy['id'], policy['tenant']))
    if any(t['state'] == 'completed' for t in transfers):
        return _reject(db, cmd, 'handed_off')
    if any(t['state'] in ('pending', 'delivered', 'armed') for t in transfers):
        # An open handoff may still be consumed by the destination; the origin
        # must not reopen until it is withdrawn or the transfer is declined.
        return _reject(db, cmd, 'handoff_pending')
    proof = None
    if cmd.get('evidence'):
        proof = _evidence(db, cmd['evidence'])
        if not proof or proof['session'] != source['id'] or proof['uid'] != source['uid'] \
                or proof['boundary'] != source['stopped_at'] \
                or proof['restore_uid'] != source['resume_uid']:
            return _reject(db, cmd, 'evidence_mismatch')
        if proof['kind'] not in ('armed', 'stoppable', 'cleared'):
            return _reject(db, cmd, 'evidence_already_used')
        if proof['kind'] == 'stoppable' and source['checkpoint'] != 'checkpoint':
            # A merely-stoppable proof did not stop this session by itself.
            return _reject(db, cmd, 'evidence_not_acted')
    elif source['checkpoint'] != 'checkpoint':
        return _reject(db, cmd, 'evidence_required')
    if not cmd.get('new_session'):
        return _reject(db, cmd, 'invalid_session')
    new_session = cmd['new_session']
    if _session(db, new_session) or new_session == source['id']:
        return _reject(db, cmd, 'duplicate_session')
    if len(_open_sessions(db, policy['id'], origin)) >= policy['seats']:
        return _reject(db, cmd, 'no_seat')
    if proof:
        db.execute("UPDATE license_evidence SET kind='resumed' WHERE id=?", (proof['id'],))
    db.execute('INSERT INTO license_sessions VALUES(?,?,?,?,?,?,?,?,?,?,?)',
               (new_session, policy['id'], cmd.get('identity') or source['identity'],
                source['uid'], 'active', origin, cmd.get('credential'), None,
                source['resume_uid'], source['id'], None))
    _accept(db, cmd, new_session, 'open',
            'resumed from %s at %s' % (source['id'], source['stopped_at']))
    return {'decision': 'session_resumed', 'session': new_session, 'uid': source['uid'],
            'cluster': origin, 'from': source['id'], 'boundary': source['stopped_at'],
            'restore_uid': source['resume_uid'], 'identity_preserved': source['identity'],
            'authority': _mode(db, policy['id'], policy['tenant']), 'issuer': origin}


def _revoke(db, cmd):
    """Withdraw a copy.  Never reopens a seat, never touches a completed one.

    * ``pending``/``delivered``: the copy has not been acted on.  Either site
      may cancel it; nothing was stopped, so the origin is unaffected.
    * ``armed``: the origin has stopped and signed the stop.  Withdrawal is too
      late on both sites, because the destination may already have consumed
      the attestation.  This is the command that must not revive a source
      session whose seat is visible at the destination.
    * ``completed``: also too late; the destination's live session stands.

    A destination that holds a ``delivered`` copy withdraws it before the
    origin arms; that is the only lawful way to give the copy back.
    """
    cluster = _cluster(db)
    transfer = _transfer(db, cmd.get('transfer'))
    if not transfer:
        return _reject(db, cmd, 'unknown_transfer')
    approval = _approval(db, transfer['approval'])
    if not approval:
        return _reject(db, cmd, 'unknown_approval')
    if cmd.get('credential') != transfer['credential']:
        return _reject(db, cmd, 'credential_mismatch')
    if cluster not in (transfer['source'], transfer['target']):
        return _reject(db, cmd, 'not_issuer')
    state = transfer['state']

    if state in ('armed', 'completed'):
        if cluster == transfer['source'] and state == 'armed':
            # Record that the origin knows, but do not change seat authority.
            _accept(db, cmd, transfer['id'], 'revoked_too_late',
                    'handoff already armed; withdrawal cannot reopen the source')
            return {'decision': 'revoked_too_late', 'transfer': transfer['id'],
                    'approval': approval['id'], 'authority': _mode(db, transfer['license'],
                                                                  transfer['tenant']),
                    'session': transfer['seat_id'], 'restore_allowed': False,
                    'issuer': transfer['target'], 'next_issuer': transfer['target']}
        _accept(db, cmd, transfer['id'], 'revoked_too_late',
                'destination already holds the seat; withdrawal has no effect')
        return {'decision': 'revoked_too_late', 'transfer': transfer['id'],
                'approval': approval['id'], 'authority': 'target',
                'session': transfer['seat_id'], 'issuer': transfer['target'],
                'restore_allowed': False, 'next_issuer': transfer['target']}

    if state == 'revoked':
        return {'decision': 'transfer_withdrawn', 'transfer': transfer['id'],
                'approval': approval['id'],
                'authority': _mode(db, transfer['license'], transfer['tenant']),
                'cluster': cluster, 'session': transfer['seat_id'],
                'restore_allowed': False, 'next_issuer': transfer['source'],
                'replay': True}

    if state == 'declined':
        return {'decision': 'transfer_deferred', 'transfer': transfer['id'],
                'approval': approval['id'],
                'authority': _mode(db, transfer['license'], transfer['tenant']),
                'cluster': cluster, 'session': transfer['seat_id'],
                'restore_allowed': False, 'next_issuer': transfer['source'],
                'replay': True}

    # pending / delivered: still withdrawable.
    db.execute("UPDATE license_transfers SET state='revoked' WHERE id=?", (transfer['id'],))
    if cmd.get('evidence_id'):
        db.execute("UPDATE license_evidence SET kind='cleared' WHERE id=?", (cmd['evidence_id'],))
    _accept(db, cmd, transfer['id'], 'transfer_withdrawn', 'copy cancelled before use')
    return {'decision': 'transfer_withdrawn', 'transfer': transfer['id'],
            'approval': approval['id'], 'authority': 'source', 'cluster': cluster,
            'session': transfer['seat_id'], 'restore_allowed': False,
            'next_issuer': transfer['source']}


# --- legacy seat operations ----------------------------------------------------

def _checkout(db, cmd, policy):
    cluster = _home(db, policy)
    state = _authority_state(db, policy, cluster)
    if not _may_issue(state, cluster):
        return _reject(db, cmd, 'not_issuer')
    if not _entitled(db, cmd.get('identity'), cmd.get('uid'), policy['tenant'], cluster):
        return _reject(db, cmd, 'not_entitled')
    if _session(db, cmd['id']):
        return _reject(db, cmd, 'duplicate_session')
    active = _open_sessions(db, policy['id'], cluster)
    if len(active) >= policy['seats']:
        return _reject(db, cmd, 'no_seat')
    db.execute('INSERT INTO license_sessions VALUES(?,?,?,?,?,?,?,?,?,?,?)',
               (cmd['id'], policy['id'], cmd['identity'], cmd['uid'], 'active', cluster,
                cmd.get('credential'), None, None, None, None))
    _accept(db, cmd, cmd['id'], 'open', 'checked out on ' + cluster)
    return {'decision': 'checked_out', 'remaining': policy['seats'] - len(active) - 1,
            'cluster': cluster, 'authority': state['mode'], 'issuer': state['issuer'] or cluster}


def _checkin(db, cmd, policy):
    cluster = _home(db, policy)
    state = _authority_state(db, policy, cluster)
    if not _may_issue(state, cluster):
        return _reject(db, cmd, 'not_issuer')
    match = next((s for s in _open_sessions(db, policy['id'], cluster)
                  if s['id'] == cmd.get('id') and s['uid'] == cmd.get('uid')), None)
    if not match:
        return _reject(db, cmd, 'unknown_session')
    db.execute("UPDATE license_sessions SET state='closed' WHERE id=?", (match['id'],))
    _accept(db, cmd, match['id'], 'closed', 'checked in')
    return {'decision': 'checked_in',
            'remaining': policy['seats'] - len(_open_sessions(db, policy['id'], cluster))}


def _checkpoint(db, cmd, policy):
    """Stop a live session at a positional boundary during a licence move.

    The instance UID is mandatory and has to match exactly.  A rebuilt identity
    that reuses the same instance name under a new UID therefore cannot act on
    (or appear to close) the session still bound to the old UID.
    """
    cluster = _home(db, policy)
    session = _session(db, cmd.get('session'))
    if not session or session['license'] != policy['id'] or session['state'] not in OPEN \
            or (session['cluster'] or cluster) != cluster:
        return _reject(db, cmd, 'unknown_session')
    if not cmd.get('uid') or cmd['uid'] != session['uid']:
        return _reject(db, cmd, 'uid_mismatch' if cmd.get('uid') else 'uid_required')
    proof = _evidence(db, cmd.get('evidence'))
    if proof and (proof['session'] != session['id'] or proof['boundary'] != cmd.get('boundary')
                  or proof['uid'] != session['uid']):
        return _reject(db, cmd, 'evidence_mismatch')
    if not isinstance(cmd.get('boundary'), int) or cmd['boundary'] < 0:
        return _reject(db, cmd, 'invalid_boundary')
    if not cmd.get('restore_uid'):
        # A stop without a named restore point could never be continued from,
        # so every checkpoint has to state one up front.
        return _reject(db, cmd, 'restore_point_required')
    db.execute("UPDATE license_sessions SET state='closed', stopped_at=?, checkpoint='checkpoint', "
               "resume_uid=? WHERE id=?", (cmd['boundary'], cmd['restore_uid'], session['id']))
    _accept(db, cmd, session['id'], 'closed', 'checkpoint at %s' % cmd['boundary'])
    return {'decision': 'session_checkpointed', 'session': session['id'],
            'uid': session['uid'], 'boundary': cmd['boundary']}


# --- probes and audit ----------------------------------------------------------

def _probe(db, cmd):
    """Prove locally, without mutating: what stopped, what may continue, who issues.

    Two agents answer separately; together their answers are the two-site
    confirmation.  A single site can never claim the other side's rows.
    """
    license_id, tenant = cmd.get('license'), cmd.get('tenant')
    policy = _policy(db, license_id, tenant)
    if not policy:
        return _reject(db, cmd, 'unknown_license')
    agent, cluster = _agent(db)
    cluster = cluster or policy['origin'] or policy['cluster']
    state = _authority_state(db, policy, cluster)
    sessions = _open_sessions(db, license_id)
    closed = rows(db, "SELECT id, uid, cluster, stopped_at, resume_uid, transferred, checkpoint "
                      "FROM license_sessions WHERE license=? AND state='closed' ORDER BY id",
                  (license_id,))
    def local_open(cluster):
        # None means this agent holds no copy of that cluster's session rows.
        if not rows(db, 'SELECT id FROM license_sessions WHERE cluster=? LIMIT 1', (cluster,)):
            return None
        return bool(_open_sessions(db, license_id, cluster))

    transfers = [{'transfer': item['id'], 'approval': item['approval'], 'state': item['state'],
                  'target': item['target'], 'bound': item['seat_id'],
                  'source_open': local_open(item['source'])}
                 for item in rows(db, 'SELECT * FROM license_transfers WHERE license=? AND tenant=? '
                                       'ORDER BY id', (license_id, tenant))]
    evidence = rows(db, 'SELECT id, session, uid, restore_uid, boundary, kind FROM license_evidence '
                        'WHERE license=? AND tenant=? ORDER BY id', (license_id, tenant))
    # A restore point exists, but is only *permitted* when the origin still owns
    # the seat and no handoff is open.  The probe reports both facts separately
    # so a stale restore record can never be mistaken for permission to resume.
    stopped = [c for c in closed if c['stopped_at'] is not None]
    restore_points = [c['resume_uid'] for c in stopped]
    resume_permitted = (
        state['mode'] == 'source' and not state['blocked']
        and state['issuer'] in (None, cluster)
        and not any(t['state'] in ('armed', 'completed') for t in transfers)
    )
    return {'decision': 'seat_state', 'agent': agent, 'cluster': cluster,
            'authority': state['mode'], 'issuer': state['issuer'],
            'may_issue': _may_issue(state, cluster), 'next_issuer': state['next'],
            'pending_handoff': state['pending'], 'armed_handoff': state['armed'],
            'open_sessions': [{'id': s['id'], 'uid': s['uid'], 'cluster': s['cluster'],
                               'state': s['state']} for s in sessions],
            'opened_here': [s['id'] for s in sessions if s['cluster'] == cluster],
            'other_cluster_open': [s['id'] for s in sessions if s['cluster'] != cluster],
            'stopped': stopped, 'restore_points': restore_points,
            'resume_permitted': resume_permitted,
            'transfers': transfers, 'evidence': evidence}


def _audit(db, cmd):
    policy = _policy(db, cmd.get('license'), cmd.get('tenant'))
    if not policy:
        return _reject(db, cmd, 'unknown_license')
    cluster = _home(db, policy)
    state = _authority_state(db, policy, cluster)
    active = _open_sessions(db, policy['id'], cluster)
    return {'decision': 'license_inventory', 'product': policy['product'],
            'used': len(active), 'remaining': policy['seats'] - len(active),
            'sessions': [{'id': s['id'], 'identity': s['identity'], 'uid': s['uid'],
                          'cluster': s['cluster'], 'state': s['state']} for s in active],
            'authority': state['mode'], 'issuer': state['issuer'] or cluster,
            'identities': rows(db, 'SELECT id, uid, cluster, state FROM license_sessions '
                                   'WHERE license=? ORDER BY id', (policy['id'],))}
