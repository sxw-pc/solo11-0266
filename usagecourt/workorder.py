"""Numbered balance-weight work orders for two-plane offline trim packs.

A work order binds a contract (two heads, the paired phases and the squared
limits) to concrete, serially numbered trim weights.  The solver derives the
directed low/high response of each head from the counts actually mounted on
its two planes, rotates the second head's response by each relative phase
before combining it on the shared base, and never substitutes a sum of
squared magnitudes for the combined squared value.

Two commission shapes are supported:

* ``a_then_b`` rotation (``uses_same_pack``): one physical pack, trialled on
  A and then on B with every serial kept in its original plane.  Only one
  machine is usable at any instant.
* ``both_available`` joint (``simultaneous``): two disjoint packs in place at
  once, accepted together on the shared stand over every contract phase.
"""
import json

from . import rotor, spinstand
from .store import one, rows

SCHEMA = """
CREATE TABLE IF NOT EXISTS workorders(id TEXT PRIMARY KEY, kind TEXT NOT NULL,
     simultaneous INTEGER NOT NULL, uses_same_pack INTEGER NOT NULL,
     heads TEXT NOT NULL, phases TEXT NOT NULL, per_head_limit INTEGER NOT NULL,
     stand_limit INTEGER NOT NULL, status TEXT NOT NULL, plan TEXT, evidence TEXT);
CREATE TABLE IF NOT EXISTS weight_claims(order_id TEXT NOT NULL, token TEXT NOT NULL,
     head TEXT, plane TEXT NOT NULL, status TEXT NOT NULL, PRIMARY KEY(order_id, token));
CREATE TABLE IF NOT EXISTS acceptance(order_id TEXT NOT NULL, slot TEXT NOT NULL,
     spin_event TEXT NOT NULL, revisions TEXT NOT NULL, PRIMARY KEY(order_id, slot));
"""

PLANES = ('a', 'b')
COUNTS = tuple((x, y) for x in range(3) for y in range(3))


def squared(pair):
    return pair[0] ** 2 + pair[1] ** 2


def directed(spec, counts, speed):
    """Directed response from the counts mounted on the two planes."""
    x, y = counts
    row = spec[speed]
    return [row['base'][i] + x * row['a'][i] + y * row['b'][i] for i in range(2)]


def individual_responses(spec, counts):
    return {speed: directed(spec, counts, speed) for speed in rotor.SPEEDS}


def paired_response(left_spec, right_spec, left_counts, right_counts, speed, phase):
    """Rotate the right head's response by the relative phase, then combine."""
    readings = [directed(left_spec, left_counts, speed),
                directed(right_spec, right_counts, speed)]
    base = spinstand.combined(*readings, phase)
    return {'readings': readings, 'base': base,
            'individual_squared': list(map(squared, readings)),
            'base_squared': squared(base)}


def split_pack(serials, counts):
    return {PLANES[0]: sorted(serials[:counts[0]]),
            PLANES[1]: sorted(serials[counts[0]:counts[0] + counts[1]])}


def available_tokens(db, exclude_order=None):
    """Stocked serials free for an order: not held by another order or mounted."""
    stocked = {row['token'] for row in db.execute('SELECT token FROM trim_inventory')}
    held = {row['token'] for row in db.execute(
        'SELECT token FROM weight_claims WHERE status=? AND order_id<>?',
        ('held', exclude_order or ''))}
    mounted = set()
    for name, in db.execute('SELECT id FROM rotor_heads'):
        head = rotor.head(db, name)
        mounted.update(head['plane_a'] + head['plane_b'])
    return sorted(stocked - held - mounted)


def _individual_failures(head, spec, counts, limit):
    failures = []
    for speed, reading in individual_responses(spec, counts).items():
        power = squared(reading)
        if power > limit:
            failures.append({'scope': 'individual', 'head': head, 'speed': speed,
                             'squared': power, 'limit': limit})
    return failures


def _paired_failures(specs, heads, left_counts, right_counts, phases, limits):
    failures = []
    per_head, stand = limits
    for phase in phases:
        for speed in rotor.SPEEDS:
            row = paired_response(specs[heads[0]], specs[heads[1]],
                                  left_counts, right_counts, speed, phase)
            if row['base_squared'] > stand:
                failures.append({'scope': 'base', 'phase': phase, 'speed': speed,
                                 'squared': row['base_squared'], 'limit': stand,
                                 'base': row['base']})
            for index, power in enumerate(row['individual_squared']):
                if power > per_head:
                    failures.append({'scope': 'individual', 'head': heads[index],
                                     'phase': phase, 'speed': speed,
                                     'squared': power, 'limit': per_head})
    return failures


def solve_rotation(db, heads, specs, phases, limits, stock):
    """Best single physical pack that qualifies on both heads in turn."""
    candidates = []
    for counts in COUNTS:
        responses = {head: individual_responses(specs[head], counts) for head in heads}
        failures = []
        for head in heads:
            failures.extend(_individual_failures(head, specs[head], counts, limits[0]))
        candidates.append({'counts': list(counts), 'tokens': sum(counts),
                           'failures': failures, 'responses': responses})
    feasible = [candidate for candidate in candidates if not candidate['failures']]
    if not feasible:
        return None, {'reason': 'envelope_exceeded', 'available': stock,
                      'candidates': candidates}
    feasible.sort(key=lambda candidate: (candidate['tokens'], candidate['counts']))
    best = feasible[0]
    if len(stock) < best['tokens']:
        return None, {'reason': 'inventory_shortage', 'available': stock,
                      'required': best['tokens'], 'best_candidate': best,
                      'candidates': candidates}
    serials = stock[:best['tokens']]
    pack = split_pack(serials, tuple(best['counts']))
    plan = {'mode': 'rotation', 'tokens': serials, 'pack': pack,
           'verification': {head: {speed: {'response': best['responses'][head][speed],
                                           'squared': squared(best['responses'][head][speed]),
                                           'limit': limits[0]}
                                   for speed in rotor.SPEEDS} for head in heads}}
    return plan, None


def solve_joint(db, heads, specs, phases, limits, stock):
    """Best pair of disjoint packs that share the base on every phase."""
    valid_a = [counts for counts in COUNTS
               if not _individual_failures(heads[0], specs[heads[0]], counts, limits[0])]
    valid_b = [counts for counts in COUNTS
               if not _individual_failures(heads[1], specs[heads[1]], counts, limits[0])]
    candidates = []
    for counts_a in valid_a:
        for counts_b in valid_b:
            failures = _paired_failures(specs, heads, counts_a, counts_b, phases, limits)
            candidates.append({'a_counts': list(counts_a), 'b_counts': list(counts_b),
                               'tokens': sum(counts_a) + sum(counts_b),
                               'failures': failures})
    feasible = [candidate for candidate in candidates if not candidate['failures']]
    if not feasible:
        return None, {'reason': 'envelope_exceeded', 'available': stock,
                      'candidates': candidates}
    feasible.sort(key=lambda candidate: (candidate['tokens'],
                                         candidate['a_counts'], candidate['b_counts']))
    best = feasible[0]
    if len(stock) < best['tokens']:
        return None, {'reason': 'inventory_shortage', 'available': stock,
                      'required': best['tokens'], 'best_candidate': best,
                      'candidates': candidates}
    serials = stock[:best['tokens']]
    packs = {heads[0]: split_pack(serials, tuple(best['a_counts'])),
             heads[1]: split_pack(serials[best['a_counts'][0] + best['a_counts'][1]:],
                                  tuple(best['b_counts']))}
    verification = {'individual': {}, 'paired': {}}
    for head, counts in ((heads[0], tuple(best['a_counts'])),
                         (heads[1], tuple(best['b_counts']))):
        verification['individual'][head] = {
            speed: {'response': directed(specs[head], counts, speed),
                    'squared': squared(directed(specs[head], counts, speed)),
                    'limit': limits[0]} for speed in rotor.SPEEDS}
    for phase in phases:
        verification['paired'][phase] = {
            speed: paired_response(specs[heads[0]], specs[heads[1]],
                                   tuple(best['a_counts']), tuple(best['b_counts']),
                                   speed, phase) for speed in rotor.SPEEDS}
    plan = {'mode': 'joint', 'tokens': serials, 'packs': packs,
           'verification': verification}
    return plan, None


def get_order(db, order_id):
    row = one(db, 'SELECT * FROM workorders WHERE id=?', (order_id,))
    if row:
        for key in ('heads', 'phases', 'plan', 'evidence'):
            if row[key] is not None:
                row[key] = json.loads(row[key])
        row['simultaneous'] = bool(row['simultaneous'])
        row['uses_same_pack'] = bool(row['uses_same_pack'])
    return row


def _contract(command):
    heads, phases = command['heads'], command['phases']
    limits = (command['per_head_limit'], command['stand_limit'])
    if (not isinstance(heads, list) or len(heads) != 2 or len(set(heads)) != 2
            or not all(isinstance(name, str) for name in heads)
            or not isinstance(phases, list) or not phases
            or any(phase not in spinstand.ROTATIONS for phase in phases)
            or any(type(limit) is not int or limit < 0 for limit in limits)
            or not isinstance(command.get('simultaneous'), bool)
            or not isinstance(command.get('uses_same_pack'), bool)):
        return None
    if not ((command['simultaneous'] and not command['uses_same_pack'])
            or (command['uses_same_pack'] and not command['simultaneous'])):
        return None
    return heads, phases, limits


def _solve(db, order):
    specs = {}
    for name in order['heads']:
        head = rotor.head(db, name)
        if head is None:
            return None, {'reason': 'unknown_head', 'head': name}
        specs[name] = head['specification']
    stock = available_tokens(db, exclude_order=order['id'])
    limits = (order['per_head_limit'], order['stand_limit'])
    if order['uses_same_pack']:
        return solve_rotation(db, order['heads'], specs, order['phases'], limits, stock)
    return solve_joint(db, order['heads'], specs, order['phases'], limits, stock)


def _open(db, command):
    contract = _contract(command)
    if contract is None or not isinstance(command.get('id'), str) or not command['id']:
        return {'decision': 'invalid_order'}
    heads, phases, limits = contract
    existing = get_order(db, command['id'])
    if existing:
        signature = (existing['kind'], existing['simultaneous'], existing['uses_same_pack'],
                     tuple(existing['heads']), tuple(existing['phases']), limits)
        asked = (command['kind'], command['simultaneous'], command['uses_same_pack'],
                tuple(heads), tuple(phases), limits)
        if signature != asked:
            return {'decision': 'order_identity_conflict'}
        return {'decision': 'order_already_open', 'order': existing['id'],
                'status': existing['status']}
    if any(rotor.head(db, name) is None for name in heads):
        return {'decision': 'unknown_head'}
    db.execute('INSERT INTO workorders VALUES(?,?,?,?,?,?,?,?,?,?,?)',
               (command['id'], command['kind'], int(command['simultaneous']),
                int(command['uses_same_pack']), json.dumps(heads), json.dumps(phases),
                limits[0], limits[1], 'refused', None, None))
    order = get_order(db, command['id'])
    plan, evidence = _solve(db, order)
    if plan is None:
        db.execute('UPDATE workorders SET evidence=? WHERE id=?',
                   (json.dumps(evidence, sort_keys=True), command['id']))
        return {'decision': 'workorder_refused', 'order': command['id'], 'evidence': evidence}
    db.execute('UPDATE workorders SET status=?, plan=? WHERE id=?',
               ('planned', json.dumps(plan, sort_keys=True), command['id']))
    return {'decision': 'workorder_planned', 'order': command['id'],
            'distinct_weights': len(plan['tokens']), 'plan': plan}


def _plan_packs(order):
    plan = order['plan']
    if plan['mode'] == 'rotation':
        return {head: plan['pack'] for head in order['heads']}
    return plan['packs']


def _issue(db, command):
    order = get_order(db, command.get('order'))
    if order is None:
        return {'decision': 'unknown_order'}
    if order['status'] == 'refused':
        return {'decision': 'order_refused'}
    if order['status'] == 'delivered':
        return {'decision': 'order_closed'}
    tokens = command.get('tokens')
    if not isinstance(tokens, list) or not tokens or len(set(tokens)) != len(tokens):
        return {'decision': 'invalid_tokens'}
    prior = rows(db, 'SELECT * FROM weight_claims WHERE order_id=?', (order['id'],))
    if prior and {row['token'] for row in prior} == set(tokens):
        return {'decision': 'tokens_already_issued', 'order': order['id'],
                'claims': prior}
    mounted = {}
    for name, in db.execute('SELECT id FROM rotor_heads'):
        head = rotor.head(db, name)
        for token in head['plane_a'] + head['plane_b']:
            mounted[token] = name
    for token in tokens:
        if not one(db, 'SELECT 1 FROM trim_inventory WHERE token=?', (token,)):
            return {'decision': 'unknown_token'}
        foreign = one(db,
            "SELECT order_id FROM weight_claims WHERE token=? AND status='held' AND order_id<>?",
            (token, order['id']))
        if foreign:
            return {'decision': 'token_already_claimed'}
        if token in mounted:
            return {'decision': 'token_not_released'}
    if set(tokens) != set(order['plan']['tokens']):
        return {'decision': 'tokens_mismatch'}
    packs = _plan_packs(order)
    claims = []
    for head, pack in packs.items():
        head_value = None if order['uses_same_pack'] else head
        for plane in PLANES:
            for token in pack[plane]:
                db.execute('INSERT OR REPLACE INTO weight_claims VALUES(?,?,?,?,?)',
                           (order['id'], token, head_value, plane, 'held'))
                claims.append({'token': token, 'head': head_value,
                               'plane': plane, 'status': 'held'})
    return {'decision': 'tokens_issued', 'order': order['id'], 'claims': claims}


def _spin_row(db, event_id):
    row = one(db, 'SELECT * FROM rotor_runs WHERE id=?', (event_id,))
    if row is None:
        return None
    return json.loads(row['output'])


def _mount_matches(head, pack):
    return head['plane_a'] == sorted(pack['a']) and head['plane_b'] == sorted(pack['b'])


def _accept(db, command):
    order = get_order(db, command.get('order'))
    if order is None:
        return {'decision': 'unknown_order'}
    if order['status'] != 'planned':
        return {'decision': f"order_{order['status']}"}
    spin = _spin_row(db, command.get('spin_event'))
    if spin is None:
        return {'decision': 'spin_event_missing'}
    if spin['decision'] != 'qualified':
        return {'decision': 'spin_not_qualified'}
    packs = _plan_packs(order)
    current = {name: rotor.head(db, name) for name in order['heads']}
    if order['uses_same_pack']:
        head = command.get('head')
        if head not in order['heads']:
            return {'decision': 'invalid_slot'}
        if spin['heads'] != [head] or spin.get('phase', '0') not in spinstand.ROTATIONS:
            return {'decision': 'spin_evidence_mismatch'}
        if not _mount_matches(current[head], packs[head]):
            return {'decision': 'pack_not_mounted'}
        if spin['revisions'] != [current[head]['revision']]:
            return {'decision': 'stale_revision'}
        slot, revisions = head, spin['revisions']
    else:
        phase = command.get('phase')
        if phase not in order['phases']:
            return {'decision': 'invalid_slot'}
        if spin['heads'] != order['heads'] or spin.get('phase') != phase:
            return {'decision': 'spin_evidence_mismatch'}
        if any(not _mount_matches(current[name], packs[name]) for name in order['heads']):
            return {'decision': 'pack_not_mounted'}
        if spin['revisions'] != [current[name]['revision'] for name in order['heads']]:
            return {'decision': 'stale_revision'}
        slot, revisions = phase, spin['revisions']
    previous = one(db, 'SELECT * FROM acceptance WHERE order_id=? AND slot=?',
                   (order['id'], slot))
    if previous:
        if previous['spin_event'] == command['spin_event']:
            return {'decision': 'acceptance_already_recorded', 'order': order['id'], 'slot': slot}
        # a remount invalidated the old receipt: the old run stays in rotor_runs,
        # the slot now points at the fresh, current-revision evidence
        db.execute('UPDATE acceptance SET spin_event=?, revisions=? WHERE order_id=? AND slot=?',
                   (command['spin_event'], json.dumps(revisions), order['id'], slot))
        return {'decision': 'acceptance_superseded', 'order': order['id'], 'slot': slot,
                'spin_event': command['spin_event'], 'revisions': revisions}
    db.execute('INSERT INTO acceptance VALUES(?,?,?,?)',
               (order['id'], slot, command['spin_event'], json.dumps(revisions)))
    return {'decision': 'acceptance_recorded', 'order': order['id'], 'slot': slot,
            'spin_event': command['spin_event'], 'revisions': revisions}


def _deliver(db, command):
    order = get_order(db, command.get('order'))
    if order is None:
        return {'decision': 'unknown_order'}
    if order['status'] == 'delivered':
        return {'decision': 'order_already_delivered', 'order': order['id']}
    if order['status'] == 'refused':
        return {'decision': 'order_refused'}
    records = rows(db, 'SELECT slot FROM acceptance WHERE order_id=?', (order['id'],))
    slots = {row['slot'] for row in records}
    packs = _plan_packs(order)
    current = {name: rotor.head(db, name) for name in order['heads']}
    if order['uses_same_pack']:
        missing = [name for name in order['heads'] if name not in slots]
        if missing:
            return {'decision': 'acceptance_incomplete', 'missing': missing}
        carrying = [name for name in order['heads'] if _mount_matches(current[name], packs[name])]
        idle = [name for name in order['heads']
                if current[name]['plane_a'] == [] and current[name]['plane_b'] == []]
        if len(carrying) != 1 or set(idle) != set(order['heads']) - set(carrying):
            return {'decision': 'pack_not_mounted'}
        receipt = {'decision': 'delivered', 'order': order['id'], 'mode': 'rotation',
                   'available_head': carrying[0], 'simultaneous_capability': False,
                   'tokens': order['plan']['tokens']}
    else:
        missing = [phase for phase in order['phases'] if phase not in slots]
        if missing:
            return {'decision': 'acceptance_incomplete', 'missing': missing}
        if any(not _mount_matches(current[name], packs[name]) for name in order['heads']):
            return {'decision': 'pack_not_mounted'}
        receipt = {'decision': 'delivered', 'order': order['id'], 'mode': 'joint',
                   'available_heads': list(order['heads']),
                   'simultaneous_capability': True, 'tokens': order['plan']['tokens']}
    db.execute("UPDATE workorders SET status='delivered' WHERE id=?", (order['id'],))
    db.execute("UPDATE weight_claims SET status='closed' WHERE order_id=?", (order['id'],))
    return receipt


def _replan(db, command):
    order = get_order(db, command.get('order'))
    if order is None:
        return {'decision': 'unknown_order'}
    if order['status'] != 'refused':
        return {'decision': 'replan_not_needed', 'status': order['status']}
    plan, evidence = _solve(db, order)
    if plan is None:
        db.execute('UPDATE workorders SET evidence=? WHERE id=?',
                   (json.dumps(evidence, sort_keys=True), order['id']))
        return {'decision': 'workorder_refused', 'order': order['id'], 'evidence': evidence}
    # a refused order carries no valid plan; drop any stale claims from an
    # earlier attempt so the fresh serial set is the only one on record
    db.execute('DELETE FROM weight_claims WHERE order_id=?', (order['id'],))
    db.execute('UPDATE workorders SET status=?, plan=?, evidence=NULL WHERE id=?',
               ('planned', json.dumps(plan, sort_keys=True), order['id']))
    return {'decision': 'workorder_planned', 'order': order['id'],
            'distinct_weights': len(plan['tokens']), 'plan': plan}


def handle(db, command):
    op = command.get('op')
    if op == 'open_order':
        return _open(db, command)
    if op == 'issue':
        return _issue(db, command)
    if op == 'accept':
        return _accept(db, command)
    if op == 'deliver':
        return _deliver(db, command)
    if op == 'replan':
        return _replan(db, command)
    if op == 'read':
        order = get_order(db, command.get('order'))
        return {'decision': 'read', 'order': order} if order else {'decision': 'unknown_order'}
    return {'decision': 'unknown_operation'}
