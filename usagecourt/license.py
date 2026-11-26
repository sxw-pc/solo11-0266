"""A simulated software inventory and per-cluster entitlement audit."""
from .store import one, rows

SCHEMA = '''
CREATE TABLE IF NOT EXISTS licenses(id TEXT PRIMARY KEY, tenant TEXT NOT NULL, product TEXT NOT NULL,
   cluster TEXT NOT NULL, seats INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS license_sessions(id TEXT PRIMARY KEY, license TEXT NOT NULL,
   identity TEXT NOT NULL, uid TEXT NOT NULL, state TEXT NOT NULL);
'''
TABLES = ('licenses', 'license_sessions')


def handle(db, cmd):
    op = cmd['op']
    if op == 'register':
        if cmd['seats'] < 1:
            return {'decision': 'invalid_seats'}
        db.execute('INSERT INTO licenses VALUES(?,?,?,?,?)', (cmd['id'], cmd['tenant'], cmd['product'],
                   cmd['cluster'], cmd['seats']))
        return {'decision': 'license_registered'}
    policy = one(db, 'SELECT * FROM licenses WHERE id=? AND tenant=?', (cmd['license'], cmd['tenant']))
    if not policy:
        return {'decision': 'unknown_license'}
    active = rows(db, "SELECT id,identity,uid FROM license_sessions WHERE license=? AND state='active' ORDER BY id",
                  (policy['id'],))
    if op == 'checkout':
        owner = one(db, 'SELECT uid,tenant,cluster FROM identities WHERE id=?', (cmd['identity'],))
        if not owner or (owner['uid'], owner['tenant'], owner['cluster']) != (cmd['uid'], policy['tenant'], policy['cluster']):
            return {'decision': 'not_entitled'}
        if len(active) >= policy['seats']:
            return {'decision': 'no_seat'}
        db.execute('INSERT INTO license_sessions VALUES(?,?,?,?,?)', (cmd['id'], policy['id'],
                   cmd['identity'], cmd['uid'], 'active'))
        return {'decision': 'checked_out', 'remaining': policy['seats']-len(active)-1}
    if op == 'checkin':
        match = next((item for item in active if item['id'] == cmd['id'] and item['uid'] == cmd['uid']), None)
        if not match:
            return {'decision': 'unknown_session'}
        db.execute("UPDATE license_sessions SET state='closed' WHERE id=?", (cmd['id'],))
        return {'decision': 'checked_in', 'remaining': policy['seats']-len(active)+1}
    if op == 'audit':
        return {'decision': 'license_inventory', 'product': policy['product'],
                'used': len(active), 'remaining': policy['seats']-len(active), 'sessions': active}
    return {'decision': 'unknown_operation'}
