"""Read-back tests of an actual fuse image in the local array simulator."""
from itertools import product
import json

from . import die

SCHEMA = """
CREATE TABLE IF NOT EXISTS die_runs(id TEXT PRIMARY KEY, output TEXT NOT NULL);
"""


def exercise(spec, image, rows, pattern, columns=None):
    columns = spec['columns'] if columns is None else columns
    logical = list(product(rows, columns))
    mapped = {cell:die.address(image,cell) for cell in logical}
    stuck = {tuple(cell) for cell in spec['stuck_zero']}
    victims = {}
    for aggressor,victim in spec['couplings']:
        victims.setdefault(tuple(aggressor),[]).append(tuple(victim))
    failures = []
    if pattern == 'isolated':
        for cell,physical in mapped.items():
            if physical in stuck:
                failures.append({'kind':'stuck_zero','cell':list(cell),'physical':list(physical)})
    else:
        reverse = {physical:cell for cell,physical in mapped.items()}
        for cell,physical in mapped.items():
            memory = dict.fromkeys(reverse,0)
            if physical not in stuck:
                memory[physical] = 1
                for victim in victims.get(physical,()):
                    if victim in memory and victim not in stuck:
                        memory[victim] ^= 1
            if memory[physical]!=1:
                failures.append({'kind':'stuck_zero','cell':list(cell),'physical':list(physical)})
            for other in memory:
                if other!=physical and memory[other]!=0:
                    failures.append({'kind':'coupled_read','aggressor':list(cell),
                                     'cell':list(reverse[other]),'physical':list(other)})
    return {'decision':'pattern_failed' if failures else 'pattern_passed',
            'pattern':pattern,'rows':rows,'columns':list(columns),'cells':len(logical),
            'failures':failures}


def handle(db, command):
    if command['op'] != 'test':
        return {'decision':'unknown_operation'}
    name = command['die']
    row = die.get(db,name)
    if not row:
        return {'decision':'unknown_die'}
    pattern = command['pattern']
    selected = command['rows']
    if (pattern not in ('isolated','shared') or not isinstance(selected,list) or not selected
            or any(type(r) is not int or r not in row['spec']['rows'] for r in selected)
            or len(set(selected))!=len(selected)):
        return {'decision':'invalid_pattern_scope'}
    columns = command.get('columns', row['spec']['columns'])
    if (not isinstance(columns,list) or not columns
            or any(type(c) is not int or c not in row['spec']['columns'] for c in columns)
            or len(set(columns))!=len(columns)):
        return {'decision':'invalid_pattern_scope'}
    result = {'die':name,'revision':row['revision'],
              **exercise(row['spec'],row['image'],sorted(selected),pattern,sorted(columns))}
    db.execute('INSERT INTO die_runs VALUES(?,?)',(command['event_id'],json.dumps(result,sort_keys=True)))
    return result
