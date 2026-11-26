"""Track sealed compute usage against a tenant's operational quota."""
from .store import one, rows

SCHEMA = '''
CREATE TABLE IF NOT EXISTS quota_pools(
  id TEXT PRIMARY KEY, tenant TEXT NOT NULL, resource TEXT NOT NULL,
  start INTEGER NOT NULL, end INTEGER NOT NULL, units INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS quota_allocations(
  pool TEXT NOT NULL, meter_case TEXT NOT NULL UNIQUE, units INTEGER NOT NULL,
  PRIMARY KEY(pool,meter_case));
'''
TABLES = ('quota_pools', 'quota_allocations')


def status(db, pool):
    allocations = rows(db, '''SELECT q.meter_case,q.units,m.status FROM quota_allocations q
        JOIN meter_cases m ON m.id=q.meter_case WHERE q.pool=? ORDER BY q.meter_case''', (pool['id'],))
    used = sum(item['units'] for item in allocations)
    return used, [item['meter_case'] for item in allocations if item['status'] != 'sealed']


def handle(db, command):
    op = command['op']
    if op == 'pool':
        if command['start'] >= command['end'] or command['units'] < 0 or command['resource'] not in ('cpu', 'memory'):
            return {'decision': 'invalid_pool'}
        db.execute('INSERT INTO quota_pools VALUES(?,?,?,?,?,?)', (command['id'], command['tenant'],
                   command['resource'], command['start'], command['end'], command['units']))
        return {'decision': 'pool_open'}
    pool = one(db, 'SELECT * FROM quota_pools WHERE id=? AND tenant=?', (command['pool'], command['tenant']))
    if not pool:
        return {'decision': 'unknown_pool'}
    used, review = status(db, pool)
    if op == 'assess':
        return {'decision': 'quota_review' if review else 'quota_current',
                'available': pool['units'] - used, 'pending_cases': review}
    if op == 'charge':
        case = one(db, '''SELECT * FROM meter_cases WHERE id=? AND tenant=? AND resource=?
            AND start>=? AND ends<=? AND status='sealed' ''',
                   (command['case'], pool['tenant'], pool['resource'], pool['start'], pool['end']))
        if not case:
            return {'decision': 'case_not_applicable'}
        prior = one(db, 'SELECT pool FROM quota_allocations WHERE meter_case=?', (case['id'],))
        if prior:
            return {'decision': 'case_already_charged', 'pool': prior['pool']}
        if review or used + case['quantity'] > pool['units']:
            return {'decision': 'quota_charge_blocked', 'available': pool['units'] - used}
        db.execute('INSERT INTO quota_allocations VALUES(?,?,?)', (pool['id'], case['id'], case['quantity']))
        return {'decision': 'quota_charged', 'available': pool['units'] - used - case['quantity']}
    return {'decision': 'unknown_operation'}
