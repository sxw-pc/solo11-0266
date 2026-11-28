"""SRAM factory delivery: plan one-time fuse substitutions, prove the delivery by
read-back, and issue a receipt that constrains downstream address access.

Only the simulated die from ``die.py`` and the local pattern model from
``patterns.py`` are used. Nothing here contacts or flashes real hardware.
"""

from itertools import combinations, permutations, product
import json

from . import die, patterns
from .store import one

SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY, die TEXT NOT NULL,
    contract TEXT NOT NULL, pattern TEXT NOT NULL, rows TEXT NOT NULL,
    spare_columns_kept INTEGER NOT NULL, revision INTEGER NOT NULL,
    physical TEXT NOT NULL, image_before TEXT NOT NULL, image TEXT NOT NULL,
    burn_run TEXT NOT NULL, test_run TEXT NOT NULL, status TEXT NOT NULL);
"""

# One die, at most four logical fuse substitutions, two patterns, local model only.
MAX_SUBSTITUTIONS = 4


def spare_assignment(origins, targets):
    """Every assignment of some origins onto distinct spare cells."""
    origins = sorted(origins)
    targets = sorted(targets)
    yield {}
    for size in range(1, min(len(origins), len(targets)) + 1):
        for picked_origins in combinations(origins, size):
            for picked_targets in permutations(targets, size):
                yield {str(o): t for o, t in zip(picked_origins, picked_targets)}


def unused_columns(image, spec):
    return [s for s in spec['spare_columns'] if s not in set(image['columns'].values())]


def candidates(actual, spec, logical, reserve_spare_columns):
    """Fuse images that extend ``actual`` without moving an occupied target.

    New substitutions are only added for origins still addressed directly in
    the delivery scope; spare cells already burned stay fixed, and enough spare
    columns are left untouched to honour the contract's reserve.
    """
    used_rows = set(actual['rows'])
    used_columns = set(actual['columns'])
    free_rows = [s for s in spec['spare_rows'] if s not in set(actual['rows'].values())]
    free_columns = [s for s in spec['spare_columns'] if s not in set(actual['columns'].values())]
    row_origins = [r for r in sorted({cell[0] for cell in logical}) if str(r) not in used_rows]
    column_origins = [c for c in spec['columns'] if str(c) not in used_columns]
    for row_image in spare_assignment(row_origins, free_rows):
        for column_image in spare_assignment(column_origins, free_columns):
            image = {'rows': {**actual['rows'], **row_image},
                     'columns': {**actual['columns'], **column_image}}
            if len(image['rows']) + len(image['columns']) > MAX_SUBSTITUTIONS:
                continue
            if len(unused_columns(image, spec)) < reserve_spare_columns:
                continue
            yield image


def evidence(spec, image, logical, pattern='shared'):
    """Check the fixed defects and the shared pattern on the physical cells.

    Every logical coordinate in the authorised delivery is resolved through the
    fuse image and checked individually (isolated) and under the read pattern.
    Mismatches retain the logical aggressor, the misread logical cell and the
    physical location.
    """
    logical = sorted(logical)
    rows = sorted({r for r, _ in logical})
    columns = sorted({c for _, c in logical})
    isolated = patterns.exercise(spec, image, rows, 'isolated', columns=columns)
    shared = patterns.exercise(spec, image, rows, pattern, columns=columns)
    physical = {f'{r},{c}': list(die.address(image, (r, c)))
                for r, c in logical}
    failures = isolated['failures'] + shared['failures']
    return {
        'decision': 'pattern_passed' if not failures else 'pattern_failed',
        'pattern': pattern,
        'rows': rows,
        'columns': columns,
        'logical': [list(cell) for cell in logical],
        'cells': len(logical),
        'physical': physical,
        'isolated': isolated['decision'],
        'shared': shared['decision'],
        'failures': failures,
    }


def _order(image):
    return (len(image['rows']) + len(image['columns']),
            len(image['rows']), len(image['columns']),
            json.dumps(image, sort_keys=True))


def plan(spec, actual, logical, pattern, reserve_spare_columns):
    """Smallest appended image that is proven to complete the delivery."""
    tried = []
    for image in sorted(candidates(actual, spec, logical, reserve_spare_columns), key=_order):
        test = evidence(spec, image, logical, pattern)
        tried.append({'image': image, 'decision': test['decision'],
                      'failures': test['failures'][:4]})
        if test['decision'] == 'pattern_passed':
            return image, test, tried
    return None, None, tried


def _parse_contract(command, spec):
    contract = command.get('contract')
    rows = command.get('rows')
    reserve = command.get('reserve_spare_columns', 0)
    pattern = command.get('pattern', 'shared')
    if (not isinstance(contract, str) or not contract
            or not isinstance(rows, list) or not rows
            or any(type(r) is not int or r not in spec['rows'] for r in rows)
            or len(set(rows)) != len(rows)
            or type(reserve) is not int or reserve < 0
            or reserve > len(spec['spare_columns'])
            or pattern not in ('isolated', 'shared')):
        return None, None, None, None
    logical = list(product(sorted(rows), spec['columns']))
    return contract, logical, pattern, reserve


def _compatible(actual, image, spec):
    if (not isinstance(image, dict) or set(image) != {'rows', 'columns'}
            or not die.validate_axis(image['rows'], spec['rows'], spec['spare_rows'])
            or not die.validate_axis(image['columns'], spec['columns'], spec['spare_columns'])):
        return False
    return all(image[axis].get(origin) == target
               for axis in ('rows', 'columns')
               for origin, target in actual[axis].items())


def _infeasible(contract, logical, actual, tried):
    best = sorted(tried, key=lambda t: (len(t['failures']), json.dumps(t['image'], sort_keys=True)))[0]
    return {'decision': 'no_feasible_image', 'contract': contract,
            'delivery': [list(cell) for cell in logical],
            'occupied': actual,
            'tried': len(tried), 'best_failure': best}


def _receipt(db, delivery):
    return one(db, 'SELECT * FROM deliveries WHERE id=?', (delivery,)) if delivery else None


def _deliver(db, command):
    name = command.get('die')
    row = die.get(db, name) if isinstance(name, str) else None
    if not row:
        return {'decision': 'unknown_die'}
    contract, logical, pattern, reserve = _parse_contract(command, row['spec'])
    if contract is None:
        return {'decision': 'invalid_contract'}
    delivery_id = command.get('id')
    if not isinstance(delivery_id, str) or not delivery_id:
        return {'decision': 'invalid_delivery'}
    existing = _receipt(db, delivery_id)
    if existing:
        same = (existing['die'] == name and existing['contract'] == contract
                and json.loads(existing['rows']) == sorted({r for r, _ in logical})
                and existing['pattern'] == pattern
                and existing['spare_columns_kept'] == reserve)
        if not same:
            return {'decision': 'delivery_identity_conflict', 'delivery': delivery_id}
        # A repeated burn event, or a lost receipt, never burns a second image.
        return {'decision': 'already_delivered', 'delivery': delivery_id,
                'revision': existing['revision'],
                'image': json.loads(existing['image'])}
    supplied = command.get('image')
    if supplied is not None:
        if not _compatible(row['image'], supplied, row['spec']):
            return {'decision': 'fuse_cannot_change', 'delivery': delivery_id,
                    'occupied': row['image'], 'supplied': supplied}
        test = evidence(row['spec'], supplied, logical, pattern)
        if test['decision'] != 'pattern_passed':
            return {'decision': 'delivery_test_failed', 'delivery': delivery_id,
                    'contract': contract, 'evidence': test}
        before = row['image']
        if supplied != row['image']:
            burn = die.apply_burn(db, name, supplied)
            if burn['decision'] != 'burned':
                return burn
        else:
            burn = {'decision': 'burned', 'revision': row['revision'], 'image': supplied}
    else:
        image, test, tried = plan(row['spec'], row['image'], logical, pattern, reserve)
        if image is None:
            return _infeasible(contract, logical, row['image'], tried)
        before = row['image']
        burn = die.apply_burn(db, name, image)
        if burn['decision'] != 'burned':
            return burn
        # Burned -- re-test the actual fuse image, not the estimate.
        test = evidence(row['spec'], burn['image'], logical, pattern)
    image = burn['image']
    if test['decision'] != 'pattern_passed':
        return {'decision': 'delivery_test_failed', 'delivery': delivery_id,
                'contract': contract, 'evidence': test}
    kept = len(unused_columns(image, row['spec']))
    if kept < reserve:
        return {'decision': 'no_feasible_image', 'contract': contract,
                'delivery': [list(cell) for cell in logical], 'occupied': before,
                'unused_spare_columns': kept}
    test_run = command.get('test_run') if isinstance(command.get('test_run'), str) else \
        f"delivery-test:{command['event_id']}"
    burn_run = command.get('burn_run') if isinstance(command.get('burn_run'), str) else \
        f"delivery-burn:{command['event_id']}"
    db.execute('INSERT OR IGNORE INTO die_runs VALUES(?,?)',
               (test_run, json.dumps(test, sort_keys=True)))
    db.execute('INSERT INTO deliveries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
               (delivery_id, name, contract, pattern,
                json.dumps(sorted({r for r, _ in logical})), reserve, burn['revision'],
                json.dumps(test['physical'], sort_keys=True),
                json.dumps(before, sort_keys=True), json.dumps(image, sort_keys=True),
                burn_run, test_run, 'delivered'))
    return {'decision': 'delivered', 'delivery': delivery_id, 'contract': contract,
            'revision': burn['revision'], 'image': image,
            'logical': [list(cell) for cell in logical],
            'unused_spare_columns': kept,
            'burn_run': burn_run, 'test_run': test_run, 'evidence': test}


def _access(db, command):
    """Downstream gate: the receipt bounds every logical read or write."""
    delivery = _receipt(db, command.get('delivery'))
    if not delivery or delivery['status'] != 'delivered':
        return {'decision': 'unknown_delivery'}
    row = die.get(db, delivery['die'])
    if not row:
        return {'decision': 'unknown_die'}
    mode = command.get('mode', 'read')
    if mode not in ('read', 'write'):
        return {'decision': 'invalid_access'}
    cell = command.get('cell')
    if (not isinstance(cell, list) or len(cell) != 2
            or any(type(v) is not int for v in cell)):
        return {'decision': 'invalid_address'}
    scope_rows = json.loads(delivery['rows'])
    valid = {(r, c) for r in scope_rows for c in row['spec']['columns']}
    if tuple(cell) not in valid:
        return {'decision': 'address_out_of_scope', 'delivery': delivery['id'],
                'mode': mode, 'cell': cell, 'rows': scope_rows}
    physical = json.loads(delivery['physical']).get(f'{cell[0]},{cell[1]}')
    if physical is None or list(die.address(row['image'], cell)) != physical:
        # A later burn re-pointed this deliverable; the receipt is stale.
        return {'decision': 'delivery_image_changed', 'delivery': delivery['id'],
                'cell': cell, 'physical': physical,
                'current': list(die.address(row['image'], cell))}
    return {'decision': 'address_allowed', 'delivery': delivery['id'],
            'mode': mode, 'cell': cell, 'physical': physical}


def handle(db, command):
    op = command['op']
    if op == 'plan':
        name = command.get('die')
        row = die.get(db, name) if isinstance(name, str) else None
        if not row:
            return {'decision': 'unknown_die'}
        contract, logical, pattern, reserve = _parse_contract(command, row['spec'])
        if contract is None:
            return {'decision': 'invalid_contract'}
        image, test, tried = plan(row['spec'], row['image'], logical, pattern, reserve)
        if image is None:
            return _infeasible(contract, logical, row['image'], tried)
        return {'decision': 'plan_ready', 'contract': contract, 'pattern': pattern,
                'rows': sorted({r for r, _ in logical}),
                'logical': [list(cell) for cell in logical],
                'unused_spare_columns': len(unused_columns(image, row['spec'])),
                'image_before': row['image'], 'image': image, 'evidence': test}
    if op == 'deliver':
        return _deliver(db, command)
    if op == 'access':
        return _access(db, command)
    return {'decision': 'unknown_operation'}
