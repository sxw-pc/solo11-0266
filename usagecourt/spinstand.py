"""Fixed-state acceptance for a simulated two-position spin stand."""
import json

from . import rotor

SCHEMA = """
CREATE TABLE IF NOT EXISTS rotor_runs(id TEXT PRIMARY KEY, output TEXT NOT NULL);
"""
ROTATIONS = {'0': (1,0), '90': (0,1), '180': (-1,0), '270': (0,-1)}


def squared(pair):
    return pair[0] ** 2 + pair[1] ** 2


def combined(left, right, phase):
    c,s = ROTATIONS[phase]
    return [left[0] + c*right[0] - s*right[1], left[1] + s*right[0] + c*right[1]]


def evaluate(heads, phase, individual_limit, stand_limit):
    by_speed = {}
    for speed in rotor.SPEEDS:
        readings = [rotor.response(head,speed) for head in heads]
        powers = list(map(squared,readings))
        base = combined(*readings, phase) if len(readings)==2 else readings[0]
        by_speed[speed] = {'readings':readings,'individual_squared':powers,
                           'base':base,'base_squared':squared(base)}
    failures = [speed for speed,row in by_speed.items()
                if max(row['individual_squared'])>individual_limit or row['base_squared']>stand_limit]
    return {'decision':'out_of_envelope' if failures else 'qualified',
            'failed_speeds':failures,'by_speed':by_speed}


def handle(db, command):
    if command['op'] != 'spin':
        return {'decision':'unknown_operation'}
    names = command['heads']
    phase = command.get('phase','0')
    individual_limit, stand_limit = command['individual_limit'], command['stand_limit']
    if (not isinstance(names,list) or not 1<=len(names)<=2 or any(not isinstance(n,str) for n in names)
            or len(set(names))!=len(names) or not isinstance(phase,str) or phase not in ROTATIONS
            or not all(type(limit) is int and limit>=0 for limit in (individual_limit,stand_limit))):
        return {'decision':'invalid_spin'}
    heads = [rotor.head(db,name) for name in names]
    if any(row is None for row in heads):
        return {'decision':'unknown_head'}
    result = {'event_id': command['event_id'], 'heads':names,'revisions':[h['revision'] for h in heads],'phase':phase,
              **evaluate(heads,phase,individual_limit,stand_limit)}
    db.execute('INSERT INTO rotor_runs VALUES(?,?)', (command['event_id'],json.dumps(result,sort_keys=True)))
    return result
