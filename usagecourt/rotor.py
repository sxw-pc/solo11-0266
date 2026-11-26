"""Simulated two-plane trim packs and rotor phasor readbacks."""
import json

from .store import one

SCHEMA = """
CREATE TABLE IF NOT EXISTS trim_inventory(token TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS rotor_heads(id TEXT PRIMARY KEY, specification TEXT NOT NULL,
     plane_a TEXT NOT NULL, plane_b TEXT NOT NULL, revision INTEGER NOT NULL);
"""
SPEEDS = ('low', 'high')


def pair(value):
    return isinstance(value, list) and len(value) == 2 and all(type(v) is int for v in value)


def specification_valid(spec):
    return (isinstance(spec, dict) and set(spec) == set(SPEEDS)
            and all(isinstance(row, dict) and set(row) == {'base', 'a', 'b'}
                    and all(pair(value) for value in row.values()) for row in spec.values()))


def head(db, name):
    row = one(db, 'SELECT * FROM rotor_heads WHERE id=?', (name,))
    if row:
        row['specification'] = json.loads(row['specification'])
        for plane in ('a','b'):
            row['plane_'+plane] = json.loads(row['plane_'+plane])
    return row


def response(head, speed):
    row = head['specification'][speed]
    x, y = len(head['plane_a']), len(head['plane_b'])
    return [row['base'][i] + x * row['a'][i] + y * row['b'][i] for i in range(2)]


def handle(db, command):
    op = command['op']
    if op == 'stock':
        token = command['token']
        if not isinstance(token,str) or not token or one(db,'SELECT 1 FROM trim_inventory WHERE token=?',(token,)):
            return {'decision':'invalid_token'}
        db.execute('INSERT INTO trim_inventory VALUES(?)',(token,))
        return {'decision':'stocked'}
    name = command['head']
    prior = head(db, name)
    if op == 'install':
        spec = command['specification']
        if prior or not specification_valid(spec):
            return {'decision':'invalid_head'}
        db.execute('INSERT INTO rotor_heads VALUES(?,?,?,?,0)',(name,json.dumps(spec,sort_keys=True),'[]','[]'))
        return {'decision':'installed','head':name,'revision':0}
    if not prior:
        return {'decision':'unknown_head'}
    if op == 'read':
        return {'decision':'read','head':name,'revision':prior['revision'],
                'a':prior['plane_a'],'b':prior['plane_b'],
                'response':{s:response(prior,s) for s in SPEEDS}}
    if op != 'mount':
        return {'decision':'unknown_operation'}
    a,b = command['a'],command['b']
    if not all(isinstance(plane,list) and len(plane)<=2 and all(isinstance(token,str) for token in plane)
               for plane in (a,b)) or len(set(a + b)) != len(a) + len(b):
        return {'decision':'invalid_pack'}
    if any(not one(db,'SELECT 1 FROM trim_inventory WHERE token=?',(token,)) for token in a+b):
        return {'decision':'unknown_token'}
    for (other,) in db.execute('SELECT id FROM rotor_heads WHERE id<>?',(name,)):
        mounted = head(db,other)
        if set(a+b).intersection(mounted['plane_a']+mounted['plane_b']):
            return {'decision':'token_already_mounted'}
    revision = prior['revision']+1
    db.execute('UPDATE rotor_heads SET plane_a=?,plane_b=?,revision=? WHERE id=?',
               (json.dumps(sorted(a)),json.dumps(sorted(b)),revision,name))
    return {'decision':'mounted','head':name,'revision':revision}
